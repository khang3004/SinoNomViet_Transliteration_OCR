#!/usr/bin/env bash
set -euo pipefail

# Initialize Qdrant collection hvb_database on K3s / Khởi tạo collection Qdrant hvb_database trên K3s
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "${ROOT}/dags/config.k3s-new" ]; then
  export KUBECONFIG="${ROOT}/dags/config.k3s-new"
fi

QDRANT_NAMESPACE="${QDRANT_NAMESPACE:-qdrant}"
QDRANT_SERVICE="${QDRANT_SERVICE:-qdrant-nodeport}"
LOCAL_PORT="${LOCAL_PORT:-16333}"

echo "Port-forward ${QDRANT_NAMESPACE}/${QDRANT_SERVICE} -> localhost:${LOCAL_PORT}"
kubectl port-forward -n "${QDRANT_NAMESPACE}" "svc/${QDRANT_SERVICE}" "${LOCAL_PORT}:6333" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "${PF_PID}" 2>/dev/null || true' EXIT
sleep 2

export HVB_QDRANT_URL="http://127.0.0.1:${LOCAL_PORT}"
export PYTHONPATH="${ROOT}/dags/jobs:${PYTHONPATH:-}"

python3 -m pip install -q qdrant-client >/dev/null 2>&1 || pip3 install -q qdrant-client

python3 "${ROOT}/dags/jobs/common/qdrant_schema.py" "$@"
