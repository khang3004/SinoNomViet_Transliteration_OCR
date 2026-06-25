#!/usr/bin/env bash
set -euo pipefail

# Read OCR JSON results from MinIO / Đọc file kết quả OCR JSON trên MinIO
# Usage:
#   bash scripts/read_ocr_results.sh list
#   bash scripts/read_ocr_results.sh cat paddle/hvb_base/page_0001.json
#   bash scripts/read_ocr_results.sh download
#   bash scripts/read_ocr_results.sh download data/output

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="${ROOT}/dags/config.ini"
if [ ! -f "$CONFIG_FILE" ]; then
  CONFIG_FILE="${ROOT}/dags/config.ini.example"
fi

get_config_val() {
  local section=$1
  local key=$2
  awk -F " *= *" -v sec="[$section]" -v k="$key" '
    $0 == sec { in_sec = 1; next }
    /^\[/ { in_sec = 0 }
    in_sec && $1 == k { print $2; exit }
  ' "$CONFIG_FILE"
}

MINIO_ENDPOINT=$(get_config_val "minio" "endpoint")
MINIO_ACCESS_KEY=$(get_config_val "minio" "access_key")
MINIO_SECRET_KEY=$(get_config_val "minio" "secret_key")
BUCKET_OUTPUT=$(get_config_val "minio" "bucket_output")
OUTPUT_DIR=$(get_config_val "paths" "output_dir")
OCR_PREFIX=$(get_config_val "minio" "ocr_output_prefix")
OCR_PREFIX="${OCR_PREFIX:-ocr}"

MINIO_PORT=$(echo "$MINIO_ENDPOINT" | grep -oE '[0-9]+$' || echo "9000")
MINIO_HOST=$(echo "$MINIO_ENDPOINT" | sed -E 's#^https?://##' | cut -d'/' -f1 | cut -d':' -f1)

mkdir -p "${ROOT}/.mc"
export MC_CONFIG_DIR="${ROOT}/.mc"

PF_PID=""
LOCAL_PF_PORT="$MINIO_PORT"
ALWAYS_FORWARD="${ALWAYS_FORWARD:-1}"
MINIO_NAMESPACE="${MINIO_NAMESPACE:-storage}"
MINIO_SERVICE="${MINIO_SERVICE:-minio}"
USE_PROJECT_KUBECONFIG="${USE_PROJECT_KUBECONFIG:-1}"

if [ "$USE_PROJECT_KUBECONFIG" = "1" ] && [ -f "${ROOT}/dags/config.k3s-new" ]; then
  export KUBECONFIG="${ROOT}/dags/config.k3s-new"
elif [ -z "${KUBECONFIG:-}" ] && [ -f "${HOME}/.kube/config.k3s-new" ]; then
  export KUBECONFIG="${HOME}/.kube/config.k3s-new"
fi

cleanup() {
  if [ -n "$PF_PID" ]; then
    kill "$PF_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

setup_port_forward() {
  if [ "$ALWAYS_FORWARD" = "1" ] || echo "$MINIO_HOST" | grep -Eq "\.svc(\.|$)"; then
    if nc -z localhost "$LOCAL_PF_PORT" &>/dev/null; then
      LOCAL_PF_PORT=19000
      while nc -z localhost "$LOCAL_PF_PORT" &>/dev/null; do
        LOCAL_PF_PORT=$((LOCAL_PF_PORT + 1))
      done
    fi

    if command -v kubectl &>/dev/null; then
      if kubectl get svc -n "$MINIO_NAMESPACE" "$MINIO_SERVICE" >/dev/null 2>&1; then
        echo "Port-forward MinIO: ${MINIO_NAMESPACE}/${MINIO_SERVICE} -> localhost:${LOCAL_PF_PORT}"
        kubectl port-forward -n "$MINIO_NAMESPACE" svc/"$MINIO_SERVICE" "$LOCAL_PF_PORT":9000 >/dev/null 2>&1 &
        PF_PID=$!
        MINIO_ENDPOINT="http://127.0.0.1:${LOCAL_PF_PORT}"
        sleep 2
      else
        SVC_LINE=$(kubectl get svc --all-namespaces 2>/dev/null | grep -iE "[[:space:]]minio([[:space:]]|$)" | head -n 1) || true
        if [ -n "$SVC_LINE" ]; then
          MINIO_NAMESPACE=$(echo "$SVC_LINE" | awk '{print $1}')
          MINIO_SERVICE=$(echo "$SVC_LINE" | awk '{print $2}')
          echo "Port-forward MinIO (detected): ${MINIO_NAMESPACE}/${MINIO_SERVICE} -> localhost:${LOCAL_PF_PORT}"
          kubectl port-forward -n "$MINIO_NAMESPACE" svc/"$MINIO_SERVICE" "$LOCAL_PF_PORT":9000 >/dev/null 2>&1 &
          PF_PID=$!
          MINIO_ENDPOINT="http://127.0.0.1:${LOCAL_PF_PORT}"
          sleep 2
        elif echo "$MINIO_HOST" | grep -Eq "\.svc(\.|$)"; then
          echo "Cannot detect MinIO service for port-forward."
          exit 1
        fi
      fi
    elif echo "$MINIO_HOST" | grep -Eq "\.svc(\.|$)"; then
      echo "kubectl is required to reach in-cluster MinIO endpoint."
      exit 1
    fi
  fi
}

require_mc() {
  if ! command -v mc &>/dev/null; then
    echo "Error: 'mc' (MinIO Client) is required. Install: brew install minio/stable/mc"
    exit 1
  fi
}

setup_mc() {
  mc alias set local-minio "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" --api s3v4 >/dev/null
}

ACTION="${1:-list}"
TARGET_PATH="local-minio/${BUCKET_OUTPUT}/${OCR_PREFIX}"

setup_port_forward
require_mc
setup_mc

case "$ACTION" in
  list)
    echo "OCR results in ${BUCKET_OUTPUT}/${OCR_PREFIX}/"
    mc ls "${TARGET_PATH}/" || true
    ;;
  cat)
    FILE_NAME="${2:-}"
    if [ -z "$FILE_NAME" ]; then
      echo "Usage: $0 cat <filename.json>"
      echo "Example: $0 cat hvb_base_paddleocr.json"
      exit 1
    fi
    # Print JSON to stdout; pipe to jq for pretty view / In JSON ra terminal; pipe jq để xem đẹp hơn
    if command -v jq &>/dev/null; then
      mc cat "${TARGET_PATH}/${FILE_NAME}" | jq .
    else
      mc cat "${TARGET_PATH}/${FILE_NAME}"
    fi
    ;;
  download)
    DEST="${2:-${ROOT}/${OUTPUT_DIR}}"
    mkdir -p "$DEST"
    echo "Downloading ${BUCKET_OUTPUT}/${OCR_PREFIX}/ -> ${DEST}/"
    mc cp --recursive "${TARGET_PATH}/" "${DEST}/"
    echo "Done. Open files in: ${DEST}"
    ;;
  *)
    echo "Usage:"
    echo "  $0 list"
    echo "  $0 cat <filename.json>"
    echo "  $0 download [local_dir]"
    exit 1
    ;;
esac
