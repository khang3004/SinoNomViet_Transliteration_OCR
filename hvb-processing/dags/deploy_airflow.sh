#!/bin/bash
set -e

# Deploy HVB DAGs to MinIO (airflow/dags/hvb-processing/)
# Upload DAG HVB lên MinIO bucket airflow

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
PROJECT_ROOT="$(cd "$DIR/.." && pwd)"

# Prioritize K3S_CONF or config.k3s-new over standard KUBECONFIG
if [ -n "$K3S_CONF" ]; then
    export KUBECONFIG="$K3S_CONF"
elif [ -f "$DIR/config.k3s-new" ]; then
    export KUBECONFIG="$DIR/config.k3s-new"
elif [ -f "$HOME/.kube/config.k3s-new" ]; then
    export KUBECONFIG="$HOME/.kube/config.k3s-new"
fi

get_config_val() {
    local section=$1
    local key=$2
    local config_file="$DIR/config.ini"
    if [ ! -f "$config_file" ]; then
        config_file="$DIR/config.ini.example"
    fi
    awk -F " *= *" -v sec="[$section]" -v k="$key" '
        $0 == sec { in_sec = 1; next }
        /^\[/ { in_sec = 0 }
        in_sec && $1 == k { print $2; exit }
    ' "$config_file"
}

MINIO_ENDPOINT=$(get_config_val "minio" "endpoint")
MINIO_ACCESS_KEY=$(get_config_val "minio" "access_key")
MINIO_SECRET_KEY=$(get_config_val "minio" "secret_key")
MINIO_BUCKET=$(get_config_val "minio" "bucket_airflow")
DAGS_PREFIX=$(get_config_val "minio" "airflow_dags_prefix")

MINIO_PORT=$(echo "$MINIO_ENDPOINT" | grep -oE '[0-9]+$' || echo "9000")
USE_PORT_FORWARD=0
MINIO_HOST=$(echo "$MINIO_ENDPOINT" | sed -E 's#^https?://##' | cut -d'/' -f1 | cut -d':' -f1)

upload_dags() {
    echo "=== Uploading HVB Airflow DAGs to MinIO (${MINIO_BUCKET}/${DAGS_PREFIX}/) ==="

    mkdir -p "$PROJECT_ROOT/.mc"
    export MC_CONFIG_DIR="$PROJECT_ROOT/.mc"

    local PF_PID=""
    local LOCAL_PF_PORT="$MINIO_PORT"

    NEED_PORT_FORWARD=0
    # Always port-forward for in-cluster service DNS / Luôn port-forward khi endpoint là DNS nội bộ cluster
    if echo "$MINIO_HOST" | grep -Eq "\.svc(\.|$)"; then
        NEED_PORT_FORWARD=1
    elif ! nc -z localhost "$MINIO_PORT" &>/dev/null; then
        NEED_PORT_FORWARD=1
    fi

    if [ "$NEED_PORT_FORWARD" -eq 1 ]; then
        # Pick a free local port for port-forward / Chọn cổng local trống để port-forward
        if nc -z localhost "$LOCAL_PF_PORT" &>/dev/null; then
            LOCAL_PF_PORT=19000
            while nc -z localhost "$LOCAL_PF_PORT" &>/dev/null; do
                LOCAL_PF_PORT=$((LOCAL_PF_PORT + 1))
            done
        fi

        echo "Setting up port-forward to MinIO service on local port $LOCAL_PF_PORT..."
        if command -v kubectl &> /dev/null; then
            local SVC_LINE=""
            SVC_LINE=$(kubectl get svc --all-namespaces 2>/dev/null | grep -i "minio" | head -n 1) || true
            if [ -n "$SVC_LINE" ]; then
                local MINIO_NAMESPACE
                local MINIO_SVC
                MINIO_NAMESPACE=$(echo "$SVC_LINE" | awk '{print $1}')
                MINIO_SVC=$(echo "$SVC_LINE" | awk '{print $2}')
                echo "Detected MinIO service: '$MINIO_SVC' in namespace '$MINIO_NAMESPACE'"
                kubectl port-forward -n "$MINIO_NAMESPACE" svc/"$MINIO_SVC" "$LOCAL_PF_PORT":9000 >/dev/null 2>&1 &
                PF_PID=$!
                USE_PORT_FORWARD=1
                MINIO_ENDPOINT="http://127.0.0.1:${LOCAL_PF_PORT}"
                echo "Port-forwarding started (PID: $PF_PID). Using endpoint: $MINIO_ENDPOINT"
                sleep 3
            else
                echo "Warning: MinIO service not found in Kubernetes."
            fi
        else
            echo "Warning: kubectl not found. Cannot set up port-forward automatically."
        fi
    else
        echo "Using configured MinIO endpoint: $MINIO_ENDPOINT"
    fi

    local EXIT_CODE=0
    local TARGET="local-minio/${MINIO_BUCKET}/dags/${DAGS_PREFIX}"

    echo "DAG layout:"
    echo "  - hvb_pdf_split_pipeline"
    echo "  - hvb_ocr_paddle_pipeline"
    echo "  - hvb_ocr_paddle_pages_pipeline"
    echo "  - hvb_ocr_gemini_pages_pipeline"
    echo "  - hvb_index_qdrant_pipeline"
    echo "  - hvb_ocr_kandianguji_pipeline"
    echo "  - hvb_ocr_google_vision_pipeline"
    echo "  - hvb_ocr_chatgpt_pipeline"
    echo "  - hvb_ocr_gemini_pipeline"
    echo "  - hvb_ocr_compare_pipeline (optional benchmark)"

    if command -v mc &> /dev/null; then
        echo "Using mc (MinIO Client) for upload..."
        mc alias set local-minio "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" --api s3v4
        mc mb "local-minio/${MINIO_BUCKET}" 2>/dev/null || true
        mc mirror --overwrite --remove \
            --exclude "*.sh" \
            --exclude ".mc*" \
            --exclude "__pycache__/*" \
            --exclude "*.pyc" \
            --exclude "config.k3s-new" \
            --exclude "config.ini.example" \
            --exclude "pipelines/__init__.py" \
            "$DIR" "$TARGET"
        echo "DAGs synchronized to ${TARGET}/ via mc (stale files removed)"
    elif command -v aws &> /dev/null; then
        echo "Using aws CLI for upload..."
        AWS_ACCESS_KEY_ID="$MINIO_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$MINIO_SECRET_KEY" \
        aws s3 sync "$DIR" "s3://${MINIO_BUCKET}/dags/${DAGS_PREFIX}" \
            --delete \
            --exclude "*.sh" \
            --exclude ".mc*" \
            --exclude "__pycache__/*" \
            --exclude "*.pyc" \
            --exclude "config.k3s-new" \
            --exclude "config.ini.example" \
            --endpoint-url "$MINIO_ENDPOINT" --no-verify-ssl
        echo "DAGs synchronized to s3://${MINIO_BUCKET}/dags/${DAGS_PREFIX}/ via aws CLI (stale files removed)"
    else
        echo "Error: Neither 'mc' nor 'aws' CLI is installed."
        EXIT_CODE=1
    fi

    if [ -n "$PF_PID" ]; then
        echo "Closing port-forward (PID: $PF_PID)..."
        kill "$PF_PID" 2>/dev/null || true
    fi

    if [ "$EXIT_CODE" -ne 0 ]; then
        exit "$EXIT_CODE"
    fi
}

ACTION=${1:-upload}
case "$ACTION" in
    upload)
        upload_dags
        ;;
    *)
        echo "Usage: ./deploy_airflow.sh upload"
        exit 1
        ;;
esac
