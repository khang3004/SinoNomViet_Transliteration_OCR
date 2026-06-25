#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-paddle}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export PYTHONPATH="${ROOT}/dags"
python3 -c "
from jobs.ocr_runner import run_batch
run_batch(model='${MODEL}', upload_minio=False)
"
