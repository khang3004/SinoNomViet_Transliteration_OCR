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


with DAG(
    dag_id="hvb_ocr_v2_pages_pipeline",
    default_args=DEFAULT_ARGS,
    description="v2 step: Gemini OCR on hvb-preprocessed PNG → hvb-ocr/{doc_id}/page_XXXX.json",
    schedule_interval=None,
    catchup=False,
    max_active_runs=1,
    tags=["hvb", "v2", "ocr", "gemini", "ramclouds", "k8s", "step"],
    params={
        "doc_id": Param(default="hvb_base", type="string"),
        "pages": Param(default="49-58", type=["null", "string"]),
        "upload_minio": Param(default=True, type="boolean"),
        "page_kind": Param(default="toc", type="string"),
        "force": Param(default=False, type="boolean"),
    },
) as dag:
    build_hvb_k8s_pod_task(
        task_id="run_ocr_v2_page_loop",
        job_name="ocr_v2_pages",
        execution_timeout=timedelta(hours=12),
        memory_limit="2Gi",
        memory_request="512Mi",
        cloud_api_secret=True,
        env_vars={
            "HVB_DOC_ID": _param_expr("doc_id"),
            "HVB_PAGES": _optional_pages_expr(),
            "HVB_PAGE_KIND": _param_expr("page_kind"),
            "HVB_FORCE": (
                "{% if (dag_run.conf.get('force', params.force) "
                "if dag_run and dag_run.conf else params.force) %}true{% else %}false{% endif %}"
            ),
            "HVB_UPLOAD_MINIO": (
                "{% if (dag_run.conf.get('upload_minio', params.upload_minio) "
                "if dag_run and dag_run.conf else params.upload_minio) %}true{% else %}false{% endif %}"
            ),
        },
    )
