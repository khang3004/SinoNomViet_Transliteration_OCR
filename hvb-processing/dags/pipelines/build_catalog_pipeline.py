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
    dag_id="hvb_build_catalog_pipeline",
    default_args=DEFAULT_ARGS,
    description="Build hvb-catalog + hvb-entries/stt_*.json from TOC OCR pages",
    schedule_interval=None,
    catchup=False,
    max_active_runs=1,
    tags=["hvb", "v2", "catalog", "stt", "k8s", "step"],
    params={
        "doc_id": Param(default="hvb_base", type="string"),
        "pages": Param(default="49-58", type=["null", "string"]),
    },
) as dag:
    build_hvb_k8s_pod_task(
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
