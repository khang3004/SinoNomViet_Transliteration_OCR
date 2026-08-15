# Han Scanner — single service with PaddleOCR (CPU)
FROM python:3.12-slim

WORKDIR /srv

# System libs needed by PaddleOCR / OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install numpy first, then paddle stack, then force numpy back
RUN pip install --no-cache-dir numpy==1.26.4 \
    && pip install --no-cache-dir \
        --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cpu/ \
        paddlepaddle==2.6.1 \
        paddleocr==2.8.1 \
        setuptools \
        fastapi==0.115.0 \
        "uvicorn[standard]==0.30.6" \
        httpx \
        Pillow==10.4.0 \
    && pip install --no-cache-dir --force-reinstall numpy==1.26.4

COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
