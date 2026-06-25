#!/usr/bin/env bash
set -euo pipefail

# Create/update K8s secret for cloud OCR API keys / Tạo secret K8s cho API key OCR cloud
# Usage: export GEMINI_API_KEY=... OPENAI_API_KEY=... && bash scripts/setup_ocr_secrets.sh

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-orchestrator}"
SECRET_NAME="${SECRET_NAME:-hvb-ocr-keys}"

if [ -f "${ROOT}/dags/config.k3s-new" ]; then
  export KUBECONFIG="${ROOT}/dags/config.k3s-new"
fi

kubectl create namespace "${NAMESPACE}" 2>/dev/null || true

ARGS=()
[ -n "${GEMINI_API_KEY:-}" ] && ARGS+=(--from-literal=HVB_GEMINI_API_KEY="${GEMINI_API_KEY}")
[ -n "${OPENAI_API_KEY:-}" ] && ARGS+=(--from-literal=HVB_OPENAI_API_KEY="${OPENAI_API_KEY}")
[ -n "${KANDIANGUJI_API_KEY:-}" ] && ARGS+=(--from-literal=HVB_KANDIANGUJI_API_KEY="${KANDIANGUJI_API_KEY}")
[ -n "${KANDIANGUJI_SERVICE_URL:-}" ] && ARGS+=(--from-literal=HVB_KANDIANGUJI_SERVICE_URL="${KANDIANGUJI_SERVICE_URL}")

if [ "${#ARGS[@]}" -eq 0 ]; then
  echo "Set at least one env var: GEMINI_API_KEY, OPENAI_API_KEY, KANDIANGUJI_API_KEY, KANDIANGUJI_SERVICE_URL"
  exit 1
fi

kubectl create secret generic "${SECRET_NAME}" -n "${NAMESPACE}" "${ARGS[@]}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret ${SECRET_NAME} updated in namespace ${NAMESPACE}."
echo "Next: patch Airflow scheduler/webserver to envFrom this secret (manual or via Helm values)."
