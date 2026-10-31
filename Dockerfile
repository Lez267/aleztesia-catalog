FROM python:3.13-slim

# Install nodejs (required for yt-dlp JS runtime) and tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

ENV PORT=8080
ENV CATALOG_HOST=0.0.0.0

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:${PORT}/healthz || exit 1

CMD ["python", "catalog_server.py"]
