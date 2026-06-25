#!/usr/bin/env bash
set -euo pipefail

# Build and deploy PaddleOCR microservice to K3s / Build và deploy microservice PaddleOCR lên K3s
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-hvb-paddle-ocr:latest}"
K8S_MANIFEST="${ROOT}/k8s/paddle-ocr.yaml"
TAR_PATH="${TAR_PATH:-/tmp/hvb-paddle-ocr.tar}"

if [ -f "${ROOT}/dags/config.k3s-new" ]; then
  export KUBECONFIG="${ROOT}/dags/config.k3s-new"
fi

require_docker() {
  if ! command -v docker &>/dev/null; then
    echo "Error: docker CLI not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
    exit 1
  fi
  if ! docker info &>/dev/null; then
    echo "Error: Docker daemon is not running."
    echo "  1. Open Docker Desktop on Mac and wait until it shows 'Running'"
    echo "  2. Run this script again: bash scripts/deploy_paddle_ocr.sh"
    exit 1
  fi
}

import_to_k3d() {
  if command -v k3d &>/dev/null && k3d cluster list 2>/dev/null | grep -q .; then
    local cluster_name
    cluster_name="$(k3d cluster list -o json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['name'])" 2>/dev/null || echo k3s-default)"
    echo "Importing image into k3d cluster: ${cluster_name}"
    k3d image import "${IMAGE_NAME}" -c "${cluster_name}"
    return 0
  fi
  return 1
}

import_to_remote_k3s_node() {
  # Import image via SSH to K3s node (remote cluster) / Import image qua SSH lên node K3s remote
  local ssh_target="${K3S_NODE_SSH:-}"
  if [ -z "${ssh_target}" ]; then
    return 1
  fi

  echo "Saving image to ${TAR_PATH}..."
  docker save "${IMAGE_NAME}" -o "${TAR_PATH}"

  echo "Copying image to ${ssh_target} and importing with k3s ctr..."
  scp "${TAR_PATH}" "${ssh_target}:/tmp/hvb-paddle-ocr.tar"
  ssh "${ssh_target}" "sudo k3s ctr images import /tmp/hvb-paddle-ocr.tar && rm -f /tmp/hvb-paddle-ocr.tar"
  rm -f "${TAR_PATH}"
  return 0
}

require_docker
echo "Building Docker image: ${IMAGE_NAME}"
docker build -t "${IMAGE_NAME}" "${ROOT}/services/paddle_ocr"

if import_to_k3d; then
  echo "Image imported into local k3d cluster."
elif import_to_remote_k3s_node; then
  echo "Image imported into remote K3s node via SSH."
else
  echo ""
  echo "WARNING: Cluster is remote — local docker image is NOT on K3s nodes yet."
  echo "Pod may stay ImagePullBackOff unless you import the image."
  echo ""
  echo "Option A — import via SSH (recommended for your setup):"
  echo "  export K3S_NODE_SSH=user@192.168.100.34   # worker node"
  echo "  bash scripts/deploy_paddle_ocr.sh"
  echo ""
  echo "Option B — push to a registry nodes can pull:"
  echo "  docker tag ${IMAGE_NAME} your-registry/hvb-paddle-ocr:latest"
  echo "  docker push your-registry/hvb-paddle-ocr:latest"
  echo "  kubectl set image deployment/hvb-paddle-ocr -n ocr paddle-ocr=your-registry/hvb-paddle-ocr:latest"
  echo ""
fi

echo "Applying Kubernetes manifest..."
kubectl apply -f "${K8S_MANIFEST}"
kubectl rollout status deployment/hvb-paddle-ocr -n ocr --timeout=300s || true

echo "PaddleOCR service: http://hvb-paddle-ocr.ocr.svc.cluster.local:8080"
echo "Health check:"
echo "  kubectl port-forward -n ocr svc/hvb-paddle-ocr 8080:8080"
echo "  curl http://localhost:8080/health"
