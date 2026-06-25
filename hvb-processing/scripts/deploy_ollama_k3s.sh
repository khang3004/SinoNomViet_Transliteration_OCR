#!/usr/bin/env bash
set -euo pipefail

# Deploy Ollama (CPU) for Paddle metadata refinement / Deploy Ollama CPU để refine metadata Paddle
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
K8S_MANIFEST="${ROOT}/k8s/ollama-kubectl.yaml"
NAMESPACE="llm"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"

if [ -f "${ROOT}/dags/config.k3s-new" ]; then
  export KUBECONFIG="${ROOT}/dags/config.k3s-new"
fi

echo "Applying Ollama Deployment (CPU-only, namespace=${NAMESPACE})..."
kubectl apply -f "${K8S_MANIFEST}"

echo "Waiting for Ollama pod..."
kubectl rollout status deployment/ollama -n "${NAMESPACE}" --timeout=300s

POD="$(kubectl get pods -n "${NAMESPACE}" -l app=ollama -o jsonpath='{.items[0].metadata.name}')"
echo "Pulling model ${OLLAMA_MODEL} (this may take several minutes)..."
kubectl exec -n "${NAMESPACE}" "${POD}" -- ollama pull "${OLLAMA_MODEL}"

echo "Done."
echo "In-cluster URL: http://ollama.${NAMESPACE}.svc.cluster.local:11434"
echo "Set in dags/config.ini:"
echo "  [ollama]"
echo "  enabled = true"
echo "  model = ${OLLAMA_MODEL}"
echo "  base_url = http://ollama.${NAMESPACE}.svc.cluster.local:11434"
