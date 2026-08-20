# Image prep — crawler records in, downloaded images + signed URLs out.
#
# No OCR: this stage does not read images, so there is no PaddleOCR, no
# paddlepaddle, and no model bake step. That is what keeps this image small
# (a few hundred MB instead of ~3 GB) and the build around a minute.

FROM python:3.12-slim

WORKDIR /srv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl is for the healthcheck. Pillow needs no extra system libs for
# JPEG/PNG/WebP on this base image.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Defaults; every one is overridable via .env (see .env.example).
ENV DATA_DIR=/data \
    BATCH_SIZE=500 \
    SCAN_INTERVAL=300 \
    DOWNLOAD_CONCURRENCY=16 \
    DOWNLOAD_TIMEOUT=30 \
    COOKIE_SECURE=1

COPY app/ ./app/

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# --workers 1 is REQUIRED, not a default worth tuning: batch state lives in this
# process, and a second worker would fork it and corrupt progress tracking.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
