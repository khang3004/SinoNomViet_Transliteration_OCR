from __future__ import annotations

from datetime import timedelta
from typing import Any

from common.config import get_value, load_config

# pyrefly: ignore [missing-import]
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator  # type: ignore
# pyrefly: ignore [missing-import]
from kubernetes.client import models as k8s  # type: ignore

WORKSPACE_VOLUME = "hvb-workspace"
WORKSPACE_MOUNT = "/workspace"
DEPS_VOLUME = "hvb-python-deps"
DEPS_MOUNT = "/opt/hvb-deps"


def _k8s_settings() -> dict[str, str]:
    # Load Kubernetes defaults from config.ini / Đọc cấu hình K8s từ config.ini
    cfg = load_config()
    return {
        "namespace": get_value(cfg, "kubernetes", "namespace", fallback="orchestrator"),
        "image": get_value(cfg, "kubernetes", "image", fallback="apache/airflow:2.10.5-python3.12"),
        "init_image": get_value(cfg, "kubernetes", "init_image", fallback="minio/mc:latest"),
        "service_account_name": get_value(
            cfg, "kubernetes", "service_account_name", fallback="default"
        ),
        "minio_endpoint": get_value(
            cfg, "minio", "endpoint", fallback="http://minio.storage.svc.cluster.local:9000"
        ),
        "dags_bucket": get_value(cfg, "minio", "bucket_airflow", fallback="airflow"),
        "dags_prefix": get_value(cfg, "minio", "airflow_dags_prefix", fallback="hvb-processing"),
        "cpu_request": get_value(cfg, "kubernetes", "pod_cpu_request", fallback="500m"),
        "cpu_limit": get_value(cfg, "kubernetes", "pod_cpu_limit", fallback="2"),
        "memory_request": get_value(cfg, "kubernetes", "pod_memory_request", fallback="1Gi"),
        "memory_limit": get_value(cfg, "kubernetes", "pod_memory_limit", fallback="4Gi"),
        "minio_secret_name": get_value(
            cfg, "kubernetes", "minio_secret_name", fallback="airflow-minio-secret"
        ),
        "ocr_secret_name": get_value(
            cfg, "kubernetes", "ocr_secret_name", fallback="hvb-ocr-keys"
        ),
        # Persistent venv PVC so pods skip reinstall / PVC venv để pod khỏi cài lại
        "deps_pvc": get_value(cfg, "kubernetes", "deps_pvc", fallback="hvb-python-deps"),
        "deps_mount": get_value(cfg, "kubernetes", "deps_mount", fallback=DEPS_MOUNT),
    }


def _minio_env_vars(settings: dict[str, str]) -> list[k8s.V1EnvVar]:
    # Inject MinIO credentials from cluster secret / Inject credential MinIO từ secret cluster
    return [
        k8s.V1EnvVar(name="MINIO_ENDPOINT", value=settings["minio_endpoint"]),
        k8s.V1EnvVar(
            name="MINIO_ACCESS_KEY",
            value_from=k8s.V1EnvVarSource(
                secret_key_ref=k8s.V1SecretKeySelector(
                    name=settings["minio_secret_name"],
                    key="MINIO_ACCESS_KEY",
                )
            ),
        ),
        k8s.V1EnvVar(
            name="MINIO_SECRET_KEY",
            value_from=k8s.V1EnvVarSource(
                secret_key_ref=k8s.V1SecretKeySelector(
                    name=settings["minio_secret_name"],
                    key="MINIO_SECRET_KEY",
                )
            ),
        ),
    ]


def _init_container(settings: dict[str, str]) -> k8s.V1Container:
    # Mirror HVB DAG code from MinIO before job starts / Đồng bộ code HVB từ MinIO trước khi chạy job
    sync_script = (
        "mc alias set minio \"$MINIO_ENDPOINT\" \"$MINIO_ACCESS_KEY\" \"$MINIO_SECRET_KEY\" --api s3v4 && "
        f"mc mirror --overwrite minio/{settings['dags_bucket']}/dags/{settings['dags_prefix']}/ "
        f"{WORKSPACE_MOUNT}/hvb-processing/ && "
        "echo 'HVB DAG sync done'"
    )
    return k8s.V1Container(
        name="sync-hvb-dags",
        image=settings["init_image"],
        command=["/bin/sh", "-c"],
        args=[sync_script],
        env=_minio_env_vars(settings),
        volume_mounts=[
            k8s.V1VolumeMount(name=WORKSPACE_VOLUME, mount_path=WORKSPACE_MOUNT),
        ],
    )


def _main_container_command(deps_mount: str) -> list[str]:
    """Install into PVC venv once (hash marker), then run job.

    Cài vào venv trên PVC một lần (theo hash requirements), lần sau bỏ qua pip.
    """
    # Cache venv on PVC; skip pip when requirements hash matches /
    # Cache venv trên PVC; bỏ pip khi hash requirements trùng
    run_script = f"""
set -euo pipefail
export HVB_CONFIG_PATH=/workspace/hvb-processing/config.ini
export HVB_PATHS_OUTPUT_DIR=/tmp/hvb-output
export HVB_SKIP_LOCAL_OUTPUT=true
export PYTHONUNBUFFERED=1

REQ_FILE=/workspace/hvb-processing/requirements.txt
DEPS_ROOT={deps_mount}
VENV_DIR="$DEPS_ROOT/venv"
MARKER="$DEPS_ROOT/.requirements.sha256"
REQ_HASH=$(sha256sum "$REQ_FILE" | awk '{{print $1}}')
mkdir -p "$DEPS_ROOT"

need_install=1
if [ -x "$VENV_DIR/bin/python" ] && [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$REQ_HASH" ]; then
  # Smoke-check a few critical imports / Smoke-check vài import quan trọng
  if "$VENV_DIR/bin/python" -c "import minio, cv2, qdrant_client, openai, fitz" >/dev/null 2>&1; then
    need_install=0
    echo "[hvb-deps] cache hit hash=$REQ_HASH — skip pip"
  else
    echo "[hvb-deps] marker ok but imports failed — reinstall"
  fi
fi

if [ "$need_install" -eq 1 ]; then
  echo "[hvb-deps] installing into $VENV_DIR (hash=$REQ_HASH)"
  # Recreate venv for clean installs / Tạo lại venv khi cần cài sạch
  rm -rf "$VENV_DIR"
  python3 -m venv --system-site-packages "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -r "$REQ_FILE"
  echo "$REQ_HASH" > "$MARKER"
  echo "[hvb-deps] install done"
fi

cd /workspace/hvb-processing/jobs
exec "$VENV_DIR/bin/python" run_k8s_job.py
""".strip()
    return ["bash", "-lc", run_script]


def build_hvb_k8s_pod_task(
    *,
    task_id: str,
    job_name: str,
    env_vars: dict[str, str],
    execution_timeout: timedelta | None = None,
    memory_limit: str | None = None,
    memory_request: str | None = None,
    cpu_limit: str | None = None,
    cpu_request: str | None = None,
    cloud_api_secret: bool = False,
    **operator_kwargs: Any,
) -> KubernetesPodOperator:
    """Create an isolated HVB job pod; keep pod only when task fails.

    Tạo pod job HVB tách biệt; chỉ giữ pod khi task failed để debug.
    """
    settings = _k8s_settings()
    deps_mount = settings["deps_mount"]
    pod_env = {
        "HVB_JOB": job_name,
        "HVB_PATHS_OUTPUT_DIR": "/tmp/hvb-output",
        "HVB_SKIP_LOCAL_OUTPUT": "true",
        **env_vars,
    }
    resources = k8s.V1ResourceRequirements(
        requests={
            "cpu": cpu_request or settings["cpu_request"],
            "memory": memory_request or settings["memory_request"],
        },
        limits={
            "cpu": cpu_limit or settings["cpu_limit"],
            "memory": memory_limit or settings["memory_limit"],
        },
    )

    run_command = _main_container_command(deps_mount)
    env_from: list[k8s.V1EnvFromSource] | None = None
    if cloud_api_secret:
        # Mount Gemini/OpenAI keys from K8s secret / Gắn API key cloud từ secret K8s
        env_from = [
            k8s.V1EnvFromSource(
                secret_ref=k8s.V1SecretEnvSource(name=settings["ocr_secret_name"])
            )
        ]
    volumes = [
        k8s.V1Volume(name=WORKSPACE_VOLUME, empty_dir=k8s.V1EmptyDirVolumeSource()),
        # Shared deps venv across sequential HVB pods / Venv deps dùng chung giữa các pod HVB tuần tự
        k8s.V1Volume(
            name=DEPS_VOLUME,
            persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
                claim_name=settings["deps_pvc"],
            ),
        ),
    ]
    volume_mounts = [
        k8s.V1VolumeMount(name=WORKSPACE_VOLUME, mount_path=WORKSPACE_MOUNT),
        k8s.V1VolumeMount(name=DEPS_VOLUME, mount_path=deps_mount),
    ]
    return KubernetesPodOperator(
        task_id=task_id,
        name=f"hvb-{job_name.replace('_', '-')}",
        namespace=settings["namespace"],
        image=settings["image"],
        cmds=run_command[:-1],
        arguments=[run_command[-1]],
        env_vars=pod_env,
        env_from=env_from,
        service_account_name=settings["service_account_name"],
        volumes=volumes,
        volume_mounts=volume_mounts,
        init_containers=[_init_container(settings)],
        container_resources=resources,
        get_logs=True,
        log_events_on_failure=True,
        on_finish_action="delete_succeeded_pod",
        startup_timeout_seconds=900,
        execution_timeout=execution_timeout or timedelta(hours=2),
        **operator_kwargs,
    )
