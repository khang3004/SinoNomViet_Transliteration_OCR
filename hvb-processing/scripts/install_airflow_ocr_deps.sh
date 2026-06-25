#!/usr/bin/env bash
set -euo pipefail

# Install HVB OCR Python deps on running Airflow scheduler / Cài dependencies OCR trên scheduler đang chạy
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-orchestrator}"
POD="${POD:-airflow-lab-scheduler-0}"
CONTAINER="${CONTAINER:-scheduler}"
REQ_PATH="/opt/airflow/dags/hvb-processing/requirements.txt"

if [ -f "${ROOT}/dags/config.k3s-new" ]; then
  export KUBECONFIG="${ROOT}/dags/config.k3s-new"
fi

echo "Installing OCR deps on ${NAMESPACE}/${POD} (${CONTAINER})..."
kubectl exec -n "${NAMESPACE}" "${POD}" -c "${CONTAINER}" -- \
  python3 -m pip install -r "${REQ_PATH}"

echo "Done. Re-trigger hvb_ocr_gemini_pipeline or hvb_ocr_chatgpt_pipeline."
