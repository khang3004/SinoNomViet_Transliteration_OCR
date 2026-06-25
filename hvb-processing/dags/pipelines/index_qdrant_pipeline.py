from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# pyrefly: ignore [missing-import]
from airflow import DAG  # noqa: F401 — required for dag_discovery_safe_mode / Bắt buộc để Airflow nhận file DAG
# pyrefly: ignore [missing-import]
from airflow.models.param import Param  # type: ignore

_JOBS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "jobs"))
if _JOBS_DIR not in sys.path:
    sys.path.append(_JOBS_DIR)

from common.k8s_operator import build_hvb_k8s_pod_task  # type: ignore

DEFAULT_ARGS = {
    "owner": "hvb",
    "depends_on_past": False,
    "start_date": datetime(2026, 6, 11),
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


def _param_expr(name: str) -> str:
    # Jinja: dag_run.conf overrides Param defaults / Jinja: conf ghi đè Param mặc định
    return (
        "{% if dag_run and dag_run.conf %}"
        f"{{{{ dag_run.conf.get('{name}', params.{name}) }}}}"
        "{% else %}"
        f"{{{{ params.{name} }}}}"
        "{% endif %}"
    )


def _optional_pages_expr() -> str:
    # Render optional pages as empty string when unset / Render pages rỗng khi không set
    return (
        "{% if dag_run and dag_run.conf %}"
        "{{ dag_run.conf.get('pages', params.pages) or '' }}"
        "{% else %}"
        "{{ params.pages or '' }}"
        "{% endif %}"
    )


with DAG(
    dag_id="hvb_index_qdrant_pipeline",
    default_args=DEFAULT_ARGS,
    description="Index per-page OCR JSON from MinIO into Qdrant (isolated K8s pod)",
    schedule_interval=None,
    catchup=False,
    max_active_runs=1,
    tags=["hvb", "index", "qdrant", "paddle", "k8s"],
    params={
        "doc_id": Param(
            default="hvb_base",
            type="string",
            description="Tài liệu cần index (vd: hvb_base).",
        ),
        "model_folder": Param(
            default="paddle",
            type="string",
            description="Folder model trên MinIO (vd: paddle, gemini).",
        ),
        "pages": Param(
            default=None,
            type=["null", "string"],
            description="(Tùy chọn) Trang: 1 | 1,3,5 | 1-10. Không điền = tất cả page_*.json.",
        ),
        "chunk_mode": Param(
            default="page",
            type="string",
            description="page = embed cả trang; block = embed từng block OCR.",
        ),
        "force_reindex": Param(
            default=False,
            type="boolean",
            description="Luôn upsert lại (idempotent).",
        ),
    },
) as dag:
    build_hvb_k8s_pod_task(
        task_id="index_minio_to_qdrant",
        job_name="index_qdrant",
        execution_timeout=timedelta(hours=4),
        memory_limit="4Gi",
        memory_request="512Mi",
        env_vars={
            "HVB_DOC_ID": _param_expr("doc_id"),
            "HVB_MODEL_FOLDER": _param_expr("model_folder"),
            "HVB_PAGES": _optional_pages_expr(),
            "HVB_CHUNK_MODE": _param_expr("chunk_mode"),
            "HVB_FORCE_REINDEX": (
                "{% if (dag_run.conf.get('force_reindex', params.force_reindex) "
                "if dag_run and dag_run.conf else params.force_reindex) %}true{% else %}false{% endif %}"
            ),
        },
    )
