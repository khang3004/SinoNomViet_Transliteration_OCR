# Han Scanner — MinIO -> Han detection -> Gemini handoff.
#
# CPU-only: the VPS has no GPU. PP-OCRv6 (paddleocr 3.7.x) is the engine — it
# claims a large CPU speedup over v5 and ships a single compact unified model,
# which matters here because RAM is the binding constraint on an 8 GB box.

FROM python:3.12-slim

WORKDIR /srv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System libraries PaddleOCR/OpenCV link against. libgomp1 is the OpenMP runtime
# paddle needs and the slim base does not carry.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install order matters. numpy first so paddle builds against the pinned version,
# then paddlepaddle from Paddle's own index, then the rest, then force numpy back
# — paddle's dependency resolution has a habit of dragging in a newer numpy that
# then breaks its own runtime.
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install numpy==1.26.4 \
    && pip install --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cpu/ \
        "paddlepaddle==3.0.0" \
    && pip install -r requirements.txt \
    && pip install --force-reinstall numpy==1.26.4

# Bake the PP-OCRv6 weights into the image.
#
# Without this, the first batch pays a model download — and worse, three OCR
# worker processes would race to download into the same cache directory at once.
# Failing the build here is deliberate: a missing model should surface now, not
# nine hours into a 20k run.
ENV PADDLE_PDX_MODEL_SOURCE=BOS
RUN python -c "\
from paddleocr import PaddleOCR; \
import numpy as np; \
ocr = PaddleOCR(lang='ch'); \
ocr.predict(np.zeros((64, 64, 3), dtype='uint8')); \
print('PP-OCRv6 weights cached')"

COPY app/ ./app/

# Defaults; every one is overridable via .env (see .env.example).
ENV DATA_DIR=/data \
    OCR_WORKERS=3 \
    OCR_LANG=ch \
    OCR_MIN_CONFIDENCE=0.3 \
    MEMORY_LIMIT_MB=6144 \
    BATCH_SIZE=500 \
    SCAN_INTERVAL=300 \
    DOWNLOAD_CONCURRENCY=16 \
    DOWNLOAD_TIMEOUT=30 \
    COOKIE_SECURE=1

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# --workers 1 is REQUIRED, not a default worth tuning: batch state lives in this
# process, and a second worker would fork it and corrupt progress tracking.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
