from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# pyrefly: ignore [missing-import]
from airflow import DAG  # type: ignore
# pyrefly: ignore [missing-import]
from airflow.models.param import Param  # type: ignore
# pyrefly: ignore [missing-import]
from airflow.operators.python import PythonOperator  # type: ignore

_JOBS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _JOBS_DIR not in sys.path:
    sys.path.append(_JOBS_DIR)

from ocr_runner import run_batch_from_split_minio  # type: ignore

DEFAULT_ARGS = {
    "owner": "hvb",
    "depends_on_past": False,
    "start_date": datetime(2026, 6, 11),
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


def create_ocr_model_dag(model_name: str, model_label: str) -> DAG:
    # Build one dedicated DAG per OCR model / Tạo một DAG riêng cho từng model OCR
    dag_id = f"hvb_ocr_{model_name}_pipeline"

    def _run_ocr(**context) -> None:
        params = context.get("params", {})
        dag_run = context.get("dag_run")
        upload_minio = params.get("upload_minio", True)
        doc_id = params.get("doc_id", "")
        pages = params.get("pages", "")
        if dag_run and dag_run.conf:
            upload_minio = dag_run.conf.get("upload_minio", upload_minio)
            doc_id = dag_run.conf.get("doc_id", doc_id)
            pages = dag_run.conf.get("pages", pages)
        run_batch_from_split_minio(
            model=model_name,
            upload_minio=upload_minio,
            doc_id=doc_id or None,
            pages=pages or None,
        )

    with DAG(
        dag_id,
        default_args=DEFAULT_ARGS,
        description=f"OCR split pages from MinIO using {model_label}",
        schedule_interval=None,
        catchup=False,
        max_active_runs=1,
        tags=["hvb", "ocr", model_name],
        params={
            "upload_minio": Param(
                default=True,
                type="boolean",
                description="Upload kết quả lên MinIO bucket hvb-ocr-result",
            ),
            "doc_id": Param(
                default="",
                type="string",
                description="Chỉ OCR tài liệu này (vd: hvb_base). Để trống = tất cả.",
            ),
            "pages": Param(
                default="",
                type="string",
                description="Trang cần OCR: 1 | 1,3,5 | 1-10. Để trống = tất cả trang.",
            ),
        },
    ) as dag:
        PythonOperator(
            task_id=f"run_ocr_{model_name}",
            python_callable=_run_ocr,
        )

    return dag
