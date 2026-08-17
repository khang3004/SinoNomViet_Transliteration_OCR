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

# paddlepaddle is NOT a pip dependency of paddleocr/paddlex — CPU and GPU builds
# are separate wheels, so pip cannot catch a version mismatch. It has to be
# pinned by hand, and it MUST stay roughly contemporaneous with paddleocr:
# the inference models are serialised in a PIR format that older runtimes cannot
# parse. Pairing paddleocr 3.7.0 with paddlepaddle 3.0.0 fails at load time with
#   ValueError: (InvalidArgument) Type of attribute: strides is not right
# Bump both together, never one alone.
ARG PADDLE_VERSION=3.3.1

# Install order matters. numpy first so paddle builds against the pinned version,
# then paddlepaddle from Paddle's own index, then the rest, then force numpy back
# — paddle's dependency resolution has a habit of dragging in a newer numpy that
# then breaks its own runtime.
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install numpy==1.26.4 \
    && pip install --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cpu/ \
        "paddlepaddle==${PADDLE_VERSION}" \
    && pip install -r requirements.txt \
    && pip install --force-reinstall numpy==1.26.4

# Bake the PP-OCRv6 weights in AND prove the runtime can actually execute them.
#
# Two jobs. Without the download, the first batch pays for it — and three OCR
# workers would race to write the same cache directory. Without the predict()
# call, a paddlepaddle/model format mismatch would sail through the build and
# only surface on the first scanned image, hours later.
#
# Failing the build here is deliberate. This step is what caught the 3.0.0
# mismatch described above.
ENV PADDLE_PDX_MODEL_SOURCE=BOS
RUN python -c "\
import numpy as np, paddle, paddleocr; \
print('paddle', paddle.__version__, '| paddleocr', paddleocr.__version__); \
from paddleocr import PaddleOCR; \
ocr = PaddleOCR(lang='ch'); \
ocr.predict(np.zeros((64, 64, 3), dtype='uint8')); \
print('PP-OCRv6 weights cached and runtime verified')"

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
