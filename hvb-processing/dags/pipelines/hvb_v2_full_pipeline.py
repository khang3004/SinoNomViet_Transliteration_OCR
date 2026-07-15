from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# pyrefly: ignore [missing-import]
from airflow import DAG  # noqa: F401
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
    return (
        "{% if dag_run and dag_run.conf %}"
        f"{{{{ dag_run.conf.get('{name}', params.{name}) }}}}"
        "{% else %}"
        f"{{{{ params.{name} }}}}"
        "{% endif %}"
    )


def _optional_pages_expr() -> str:
    return (
        "{% if dag_run and dag_run.conf %}"
        "{{ dag_run.conf.get('pages', params.pages) or '' }}"
        "{% else %}"
        "{{ params.pages or '' }}"
        "{% endif %}"
    )


def _bool_env_expr(name: str) -> str:
    return (
        "{% if (dag_run.conf.get('"
        + name
        + "', params."
        + name
        + ") if dag_run and dag_run.conf else params."
        + name
        + ") %}true{% else %}false{% endif %}"
    )


with DAG(
    dag_id="hvb_v2_full_pipeline",
    default_args=DEFAULT_ARGS,
    description=(
        "v2.3 TOC/STT: preprocess → OCR(toc) → catalog → refine → stitch(orphan absorb) → index. "
        "Orphan head ở đầu trang → upsert STT gần nhất trang trước (kể cả biên batch). force=true to re-OCR."
    ),
    schedule_interval=None,
    catchup=False,
    max_active_runs=1,
    tags=["hvb", "v2", "toc", "catalog", "stt", "refine", "qdrant", "full", "k8s"],
    params={
        "doc_id": Param(default="hvb_base", type="string"),
        "pages": Param(default="49-58", type=["null", "string"]),
        "page_kind": Param(default="toc", type="string"),
        "force": Param(default=False, type="boolean"),
        "qdrant_recreate": Param(default=True, type="boolean"),
    },
) as dag:
    preprocess = build_hvb_k8s_pod_task(
        task_id="opencv_preprocess_pages",
        job_name="opencv_preprocess_pages",
        execution_timeout=timedelta(hours=12),
        memory_limit="4Gi",
        memory_request="1Gi",
        env_vars={
            "HVB_DOC_ID": _param_expr("doc_id"),
            "HVB_PAGES": _optional_pages_expr(),
            "HVB_UPLOAD_MINIO": "true",
            "HVB_FORCE": _bool_env_expr("force"),
        },
    )
    ocr = build_hvb_k8s_pod_task(
        task_id="ocr_v2_pages",
        job_name="ocr_v2_pages",
        execution_timeout=timedelta(hours=12),
        memory_limit="2Gi",
        memory_request="512Mi",
        cloud_api_secret=True,
        env_vars={
            "HVB_DOC_ID": _param_expr("doc_id"),
            "HVB_PAGES": _optional_pages_expr(),
            "HVB_UPLOAD_MINIO": "true",
            "HVB_PAGE_KIND": _param_expr("page_kind"),
            "HVB_FORCE": _bool_env_expr("force"),
        },
    )
    catalog = build_hvb_k8s_pod_task(
        task_id="build_catalog",
        job_name="build_catalog",
        execution_timeout=timedelta(hours=2),
        memory_limit="1Gi",
        memory_request="256Mi",
        env_vars={
            "HVB_DOC_ID": _param_expr("doc_id"),
            "HVB_PAGES": _optional_pages_expr(),
            "HVB_UPLOAD_MINIO": "true",
        },
    )
    refine = build_hvb_k8s_pod_task(
        task_id="refine_entries",
        job_name="refine_entries",
        execution_timeout=timedelta(hours=6),
        memory_limit="2Gi",
        memory_request="512Mi",
        cloud_api_secret=True,
        env_vars={
            "HVB_DOC_ID": _param_expr("doc_id"),
            "HVB_PAGES": _optional_pages_expr(),
            "HVB_UPLOAD_MINIO": "true",
            "HVB_FORCE": "false",
        },
    )
    stitch = build_hvb_k8s_pod_task(
        task_id="stitch_entries",
        job_name="stitch_entries",
        execution_timeout=timedelta(hours=4),
        memory_limit="2Gi",
        memory_request="512Mi",
        cloud_api_secret=True,
        env_vars={
            "HVB_DOC_ID": _param_expr("doc_id"),
            "HVB_PAGES": _optional_pages_expr(),
            "HVB_UPLOAD_MINIO": "true",
        },
    )
    index_catalog = build_hvb_k8s_pod_task(
        task_id="index_catalog_qdrant",
        job_name="index_catalog_qdrant",
        execution_timeout=timedelta(hours=4),
        memory_limit="4Gi",
        memory_request="512Mi",
        env_vars={
            "HVB_DOC_ID": _param_expr("doc_id"),
            "HVB_PAGES": _optional_pages_expr(),
            "HVB_QDRANT_RECREATE": _bool_env_expr("qdrant_recreate"),
        },
    )

    preprocess >> ocr >> catalog >> refine >> stitch >> index_catalog
