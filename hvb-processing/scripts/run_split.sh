#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export PYTHONPATH="${ROOT}/dags"
python3 -c "
from jobs.split_pdf import split_and_upload_batch_from_minio, sync_local_raw_to_minio
sync_local_raw_to_minio()
split_and_upload_batch_from_minio()
"
