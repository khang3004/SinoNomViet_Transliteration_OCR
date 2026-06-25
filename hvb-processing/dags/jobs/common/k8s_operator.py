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


def _main_container_command() -> list[str]:
    # Install deps and run HVB job CLI / Cài dependency và chạy CLI job HVB
    run_script = (
        "set -euo pipefail && "
        "export HVB_CONFIG_PATH=/workspace/hvb-processing/config.ini && "
        "export HVB_PATHS_OUTPUT_DIR=/tmp/hvb-output && "
        "export HVB_SKIP_LOCAL_OUTPUT=true && "
        "export PYTHONUNBUFFERED=1 && "
        "pip install --no-cache-dir -q -r /workspace/hvb-processing/requirements.txt && "
        "cd /workspace/hvb-processing/jobs && "
        "exec python3 run_k8s_job.py"
    )
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

    run_command = _main_container_command()
    env_from: list[k8s.V1EnvFromSource] | None = None
    if cloud_api_secret:
        # Mount Gemini/OpenAI keys from K8s secret / Gắn API key cloud từ secret K8s
        env_from = [
            k8s.V1EnvFromSource(
                secret_ref=k8s.V1SecretEnvSource(name=settings["ocr_secret_name"])
            )
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
        volumes=[k8s.V1Volume(name=WORKSPACE_VOLUME, empty_dir=k8s.V1EmptyDirVolumeSource())],
        volume_mounts=[
            k8s.V1VolumeMount(name=WORKSPACE_VOLUME, mount_path=WORKSPACE_MOUNT),
        ],
        init_containers=[_init_container(settings)],
        container_resources=resources,
        get_logs=True,
        log_events_on_failure=True,
        on_finish_action="delete_succeeded_pod",
        startup_timeout_seconds=900,
        execution_timeout=execution_timeout or timedelta(hours=2),
        **operator_kwargs,
    )
