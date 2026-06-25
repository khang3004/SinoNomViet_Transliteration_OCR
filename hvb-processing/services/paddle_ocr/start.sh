#!/bin/bash
set -euo pipefail

# Install runtime deps and start PaddleOCR API / Cài dependency runtime và khởi động API PaddleOCR
echo "Installing system packages..."
apt-get update
apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1
rm -rf /var/lib/apt/lists/*

echo "Installing PaddlePaddle (${PADDLE_DEVICE:-cpu})..."
# CPU vs GPU wheel is chosen at deploy time / Chọn wheel CPU hoặc GPU lúc deploy
if [ "${PADDLE_DEVICE:-cpu}" = "gpu" ]; then
  PADDLE_GPU_INDEX="${PADDLE_GPU_INDEX:-https://www.paddlepaddle.org.cn/packages/stable/cu126/}"
  pip install --no-cache-dir "paddlepaddle-gpu==3.2.2" -i "${PADDLE_GPU_INDEX}"
else
  pip install --no-cache-dir "paddlepaddle==3.2.2"
fi

echo "Installing Python packages (may take several minutes)..."
pip install --no-cache-dir -r /app/requirements.txt

export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="${PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK:-True}"

echo "Starting PaddleOCR service on :${PADDLE_OCR_PORT:-8080}"
cd /app
exec uvicorn app:app --host 0.0.0.0 --port "${PADDLE_OCR_PORT:-8080}"
