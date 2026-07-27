FROM python:3.11-slim

# Install FFmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# App directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY index.html .

# Create persistent storage directories
RUN mkdir -p /data/videos

# Fly.io application port
EXPOSE 8080

# Start FastAPI
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
