#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export PYTHONPATH="${ROOT}/dags"
python3 -c "
from jobs.ocr_runner import run_compare
run_compare(models=['paddle', 'google_vision', 'chatgpt', 'gemini', 'kandianguji'], upload_minio=False)
"
