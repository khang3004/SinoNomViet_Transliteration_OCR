#!/usr/bin/env bash
set -euo pipefail

# Deploy PaddleOCR on K3s without local Docker / Deploy PaddleOCR lên K3s không cần Docker trên Mac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${ROOT}/services/paddle_ocr"
USE_GPU=false
if [ "${1:-}" = "--gpu" ]; then
  USE_GPU=true
  shift
fi
if [ "${USE_GPU}" = true ]; then
  K8S_MANIFEST="${ROOT}/k8s/paddle-ocr-gpu-kubectl.yaml"
else
  K8S_MANIFEST="${ROOT}/k8s/paddle-ocr-kubectl.yaml"
fi
NAMESPACE="ocr"
PADDLE_OCR_PORT="${PADDLE_OCR_PORT:-8080}"

if [ -f "${ROOT}/dags/config.k3s-new" ]; then
  export KUBECONFIG="${ROOT}/dags/config.k3s-new"
fi

for file in app.py requirements.txt start.sh text_layout.py block_merge.py; do
  if [ ! -f "${APP_DIR}/${file}" ]; then
    echo "Missing file: ${APP_DIR}/${file}"
    exit 1
  fi
done

echo "Creating namespace ${NAMESPACE} (if needed)..."
kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

echo "Uploading app source as ConfigMap..."
kubectl create configmap hvb-paddle-ocr-app -n "${NAMESPACE}" \
  --from-file=app.py="${APP_DIR}/app.py" \
  --from-file=block_merge.py="${APP_DIR}/block_merge.py" \
  --from-file=text_layout.py="${APP_DIR}/text_layout.py" \
  --from-file=requirements.txt="${APP_DIR}/requirements.txt" \
  --from-file=start.sh="${APP_DIR}/start.sh" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Applying Deployment + Service (port ${PADDLE_OCR_PORT}, gpu=${USE_GPU}, replicas=${PADDLE_REPLICAS:-auto})..."
python3 - <<PY
from pathlib import Path
import re
port = "${PADDLE_OCR_PORT}"
text = Path("${K8S_MANIFEST}").read_text()
text = re.sub(r'value: "8080"', f'value: "{port}"', text, count=1)
text = re.sub(r"containerPort: 8080", f"containerPort: {port}", text, count=1)
text = re.sub(r"port: 8080\n      targetPort: 8080", f"port: {port}\n      targetPort: {port}", text, count=1)
Path("/tmp/paddle-ocr-kubectl.rendered.yaml").write_text(text)
PY
kubectl apply -f /tmp/paddle-ocr-kubectl.rendered.yaml

echo "Waiting for pod rollout (pip install may take 5-15 minutes on first start)..."
kubectl rollout status deployment/hvb-paddle-ocr -n "${NAMESPACE}" --timeout=900s || {
  echo "Rollout not ready yet. Check logs:"
  echo "  kubectl logs -n ${NAMESPACE} deploy/hvb-paddle-ocr -f"
  exit 1
}

echo "Done."
echo "Service URL (in cluster): http://hvb-paddle-ocr.ocr.svc.cluster.local:${PADDLE_OCR_PORT}"
echo "Update dags/config.ini:"
echo "  [paddle] service_url = http://hvb-paddle-ocr.ocr.svc.cluster.local:${PADDLE_OCR_PORT}"
echo "Health check from laptop:"
echo "  kubectl port-forward -n ${NAMESPACE} svc/hvb-paddle-ocr ${PADDLE_OCR_PORT}:${PADDLE_OCR_PORT}"
echo "  curl http://localhost:${PADDLE_OCR_PORT}/health"
