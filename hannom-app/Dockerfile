# OCR Review — the upstream team's transcriptions in, audited verdicts out.
#
# No models and no image processing beyond reading dimensions, so this stays a
# small pure-Python image that builds in about a minute.

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
    SAMPLE_BATCH=100 \
    SAMPLE_PER_POST_CAP=2 \
    IMAGE_URL_TTL_DAYS=7 \
    COOKIE_SECURE=1

COPY app/ ./app/

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# --workers 1 is REQUIRED, not a default worth tuning: assignment is serialised
# by an in-process lock, and a second worker would hand the same image to two
# reviewers.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
