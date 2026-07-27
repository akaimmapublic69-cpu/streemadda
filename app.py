import os
import uuid
import time
import shutil
import asyncio
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# =========================================================
# CONFIG
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

# Fly.io persistent volume
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))

# Local fallback if /data does not exist
if not DATA_DIR.exists():
    DATA_DIR = BASE_DIR / "data"

VIDEO_DIR = DATA_DIR / "videos"
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

MAX_VIDEO_SIZE = 4 * 1024 * 1024 * 1024  # 4GB


# =========================================================
# APP
# =========================================================

app = FastAPI(title="StreamAdda")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# DATA
# =========================================================

streams = {}
lock = threading.Lock()


# =========================================================
# PLATFORM RTMP
# =========================================================

def get_rtmp_url(platform: str):
    platform = (platform or "").lower().strip()

    if platform == "youtube":
        return "rtmps://a.rtmps.youtube.com/live2"

    if platform == "facebook":
        return "rtmps://live-api-s.facebook.com:443/rtmp"

    raise ValueError("Unsupported platform")


# =========================================================
# MODELS
# =========================================================

class StreamCreate(BaseModel):
    name: str = "Live Stream"
    ratio: str = "9:16"
    video_path: str
    filename: str = ""
    total_minutes: int
    platform: str
    stream_key: str
    backup_rtmp: str = ""
    start_at: str = ""
    stop_at: str = ""


class StreamPatch(BaseModel):
    stop_at: str = ""


# =========================================================
# HELPERS
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def parse_datetime(value):
    if not value:
        return None

    try:
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt
    except Exception:
        return None


def safe_filename(name: str):
    name = Path(name).name

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789"
        "._-"
    )

    return "".join(c if c in allowed else "_" for c in name)


def get_video_path(path_string: str):
    """
    Only allow video files inside /data/videos.
    """

    if not path_string:
        return None

    path = Path(path_string)

    # Absolute path
    if path.is_absolute():
        real = path.resolve()
    else:
        real = (BASE_DIR / path).resolve()

    video_root = VIDEO_DIR.resolve()

    try:
        real.relative_to(video_root)
    except ValueError:
        return None

    if not real.exists():
        return None

    return real


def format_stream(info):
    return {
        "id": info["id"],
        "name": info["name"],
        "status": info["status"],
        "ratio": info["ratio"],
        "filename": info.get("filename", ""),
        "total_minutes": info["total_minutes"],
        "uptime": int(time.time() - info["started_timestamp"])
        if info.get("started_timestamp")
        else 0,
        "current_phase": info.get("current_phase"),
        "phase_remaining": info.get("phase_remaining"),
        "photo_duration": 0,
        "video_duration": info.get("video_duration", 0),
        "stop_at": info.get("stop_at", ""),
        "platform": info.get("platform", ""),
    }


def add_log(stream_id, message):
    message = str(message).strip()

    if not message:
        return

    info = streams.get(stream_id)

    if not info:
        return

    line = f"[SA] {message}"

    with lock:
        info["logs"].append(line)

        if len(info["logs"]) > 300:
            info["logs"] = info["logs"][-300:]

        for q in info["listeners"]:
            try:
                q.put_nowait(line)
            except Exception:
                pass


# =========================================================
# FFMPEG
# =========================================================

def build_ffmpeg_command(info):
    video_path = info["video_path"]
    stream_key = info["stream_key"]
    platform = info["platform"]

    main_rtmp = get_rtmp_url(platform)

    output_main = f"{main_rtmp}/{stream_key}"

    outputs = [
        "-f",
        "flv",
        output_main,
    ]

    # Backup RTMP
    backup = info.get("backup_rtmp", "").strip()

    if backup:
        outputs.extend([
            "-f",
            "flv",
            backup,
        ])

    # Ratio
    if info["ratio"] == "16:9":
        width = 1280
        height = 720
    else:
        width = 720
        height = 1280

    # Total stream duration
    duration_seconds = info["total_minutes"] * 60

    command = [
        "ffmpeg",

        "-hide_banner",

        # Loop video infinitely
        "-stream_loop",
        "-1",

        "-re",

        "-i",
        str(video_path),

        # Video scaling
        "-vf",
        (
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        ),

        # Video codec
        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-tune",
        "zerolatency",

        "-pix_fmt",
        "yuv420p",

        "-r",
        "30",

        "-g",
        "60",

        # Audio
        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-ar",
        "44100",

        # Duration
        "-t",
        str(duration_seconds),

        # FLV output
    ]

    # Main output
    command.extend([
        "-f",
        "flv",
        output_main,
    ])

    return command


def run_ffmpeg(stream_id):
    info = streams.get(stream_id)

    if not info:
        return

    try:
        info["status"] = "starting"
        add_log(stream_id, "FFmpeg starting...")

        command = build_ffmpeg_command(info)

        add_log(stream_id, "Video loop enabled.")
        add_log(
            stream_id,
            f"Total stream duration: {info['total_minutes']} minutes"
        )

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        info["process"] = process
        info["status"] = "online"
        info["current_phase"] = "video_phase"
        info["started_timestamp"] = time.time()

        add_log(stream_id, "Stream is LIVE.")

        while True:

            # Scheduled stop check
            stop_at = parse_datetime(info.get("stop_at", ""))

            if stop_at and now_utc() >= stop_at:
                add_log(stream_id, "Scheduled stop time reached.")
                break

            line = process.stdout.readline()

            if line:
                line = line.strip()

                if line:
                    add_log(stream_id, line)

            if process.poll() is not None:
                break

            time.sleep(0.05)

        # Stop FFmpeg
        if process.poll() is None:
            process.terminate()

            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

        info["status"] = "offline"
        info["current_phase"] = None
        info["process"] = None

        add_log(stream_id, "Stream stopped.")

    except Exception as e:

        info["status"] = "error"
        info["current_phase"] = None
        info["process"] = None

        add_log(stream_id, f"ERROR: {str(e)}")


def start_stream_thread(stream_id):
    thread = threading.Thread(
        target=run_ffmpeg,
        args=(stream_id,),
        daemon=True,
    )

    thread.start()


# =========================================================
# SCHEDULE WORKER
# =========================================================

def scheduler_loop():

    while True:

        try:

            for stream_id, info in list(streams.items()):

                if info["status"] != "scheduled":
                    continue

                start_at = parse_datetime(info.get("start_at", ""))

                if not start_at:
                    continue

                if now_utc() >= start_at:

                    add_log(
                        stream_id,
                        "Scheduled start time reached."
                    )

                    start_stream_thread(stream_id)

        except Exception:
            pass

        time.sleep(5)


scheduler_thread = threading.Thread(
    target=scheduler_loop,
    daemon=True,
)

scheduler_thread.start()


# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup_event():

    print("====================================")
    print("StreamAdda Backend Started")
    print("Video Directory:", VIDEO_DIR)
    print("====================================")


# =========================================================
# FRONTEND
# =========================================================

@app.get("/")
async def home():

    index_file = BASE_DIR / "index.html"

    if not index_file.exists():
        return {
            "message": "StreamAdda API is running",
            "status": "ok",
        }

    return FileResponse(index_file)


# =========================================================
# HEALTH
# =========================================================

@app.get("/api/health")
async def health():

    ffmpeg_ok = shutil.which("ffmpeg") is not None

    return {
        "ok": True,
        "ffmpeg": ffmpeg_ok,
        "video_dir": str(VIDEO_DIR),
    }


# =========================================================
# VIDEO UPLOAD
# =========================================================

@app.post("/api/upload/video")
async def upload_video(file: UploadFile = File(...)):

    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="No video file selected."
        )

    original_name = safe_filename(file.filename)

    extension = Path(original_name).suffix.lower()

    allowed_extensions = {
        ".mp4",
        ".mkv",
        ".mov",
        ".avi",
        ".webm",
        ".m4v",
    }

    if extension not in allowed_extensions:

        raise HTTPException(
            status_code=400,
            detail="Unsupported video format."
        )

    unique_name = f"{uuid.uuid4().hex}{extension}"

    destination = VIDEO_DIR / unique_name

    total_size = 0

    try:

        with open(destination, "wb") as buffer:

            while True:

                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_VIDEO_SIZE:

                    destination.unlink(
                        missing_ok=True
                    )

                    raise HTTPException(
                        status_code=413,
                        detail="Video exceeds 4GB limit."
                    )

                buffer.write(chunk)

    except HTTPException:
        raise

    except Exception as e:

        destination.unlink(
            missing_ok=True
        )

        raise HTTPException(
            status_code=500,
            detail=f"Upload failed: {str(e)}"
        )

    return {
        "ok": True,
        "path": str(destination),
        "filename": original_name,
        "size": total_size,
    }


# =========================================================
# STREAM LIST
# =========================================================

@app.get("/api/streams")
async def get_streams():

    return {
        stream_id: format_stream(info)
        for stream_id, info in streams.items()
    }


# =========================================================
# CREATE STREAM
# =========================================================

@app.post("/api/streams")
async def create_stream(data: StreamCreate):

    if not data.stream_key.strip():

        raise HTTPException(
            status_code=400,
            detail="Stream key required."
        )

    if data.total_minutes < 2:

        raise HTTPException(
            status_code=400,
            detail="Minimum duration is 2 minutes."
        )

    video_path = get_video_path(data.video_path)

    if not video_path:

        raise HTTPException(
            status_code=400,
            detail="Video file not found."
        )

    stream_id = uuid.uuid4().hex[:12]

    start_at = data.start_at.strip()
    stop_at = data.stop_at.strip()

    status = "offline"

    start_datetime = parse_datetime(start_at)

    if start_datetime and start_datetime > now_utc():

        status = "scheduled"

    info = {
        "id": stream_id,
        "name": data.name.strip() or "Live Stream",
        "ratio": data.ratio,
        "video_path": str(video_path),
        "filename": data.filename,
        "total_minutes": data.total_minutes,
        "platform": data.platform,
        "stream_key": data.stream_key,
        "backup_rtmp": data.backup_rtmp,
        "start_at": start_at,
        "stop_at": stop_at,
        "status": status,
        "process": None,
        "started_timestamp": None,
        "current_phase": None,
        "phase_remaining": None,
        "video_duration": 0,
        "logs": [],
        "listeners": [],
    }

    streams[stream_id] = info

    add_log(
        stream_id,
        f"Stream created: {info['name']}"
    )

    if status == "scheduled":

        add_log(
            stream_id,
            f"Scheduled start: {start_at}"
        )

    else:

        start_stream_thread(stream_id)

    return format_stream(info)


# =========================================================
# STOP STREAM
# =========================================================

@app.post("/api/streams/{stream_id}/stop")
async def stop_stream(stream_id: str):

    info = streams.get(stream_id)

    if not info:

        raise HTTPException(
            status_code=404,
            detail="Stream not found."
        )

    process = info.get("process")

    if process and process.poll() is None:

        try:
            process.terminate()

        except Exception:
            pass

    info["status"] = "offline"
    info["current_phase"] = None

    add_log(
        stream_id,
        "Manual stop requested."
    )

    return {
        "ok": True,
        "status": "offline",
    }


# =========================================================
# UPDATE STOP SCHEDULE
# =========================================================

@app.patch("/api/streams/{stream_id}")
async def update_stream(
    stream_id: str,
    data: StreamPatch
):

    info = streams.get(stream_id)

    if not info:

        raise HTTPException(
            status_code=404,
            detail="Stream not found."
        )

    if data.stop_at:

        stop_datetime = parse_datetime(data.stop_at)

        if not stop_datetime:

            raise HTTPException(
                status_code=400,
                detail="Invalid stop time."
            )

        if stop_datetime <= now_utc():

            raise HTTPException(
                status_code=400,
                detail="Stop time must be in the future."
            )

    info["stop_at"] = data.stop_at

    add_log(
        stream_id,
        "Stop schedule updated."
    )

    return format_stream(info)


# =========================================================
# DELETE STREAM
# =========================================================

@app.delete("/api/streams/{stream_id}")
async def delete_stream(stream_id: str):

    info = streams.get(stream_id)

    if not info:

        raise HTTPException(
            status_code=404,
            detail="Stream not found."
        )

    process = info.get("process")

    if process and process.poll() is None:

        try:
            process.terminate()

        except Exception:
            pass

    video_path = info.get("video_path")

    if video_path:

        try:
            path = Path(video_path)

            if path.exists():
                path.unlink()

        except Exception:
            pass

    del streams[stream_id]

    return {
        "ok": True
    }


# =========================================================
# SSE FFmpeg LOGS
# =========================================================

@app.get("/api/streams/{stream_id}/events")
async def stream_events(stream_id: str):

    info = streams.get(stream_id)

    if not info:

        raise HTTPException(
            status_code=404,
            detail="Stream not found."
        )

    queue = asyncio.Queue()

    # Send previous logs
    for line in info.get("logs", [])[-30:]:

        try:
            queue.put_nowait(line)

        except Exception:
            pass

    info["listeners"].append(queue)

    async def event_generator():

        try:

            while True:

                try:

                    line = await asyncio.wait_for(
                        queue.get(),
                        timeout=15,
                    )

                    yield f"data: {line}\n\n"

                except asyncio.TimeoutError:

                    yield ": keepalive\n\n"

        finally:

            try:
                info["listeners"].remove(queue)

            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
