from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# pyrefly: ignore [missing-import]
from airflow import DAG  # type: ignore
# pyrefly: ignore [missing-import]
from airflow.operators.python import PythonOperator  # type: ignore

# Allow importing from jobs folder / Cho phép import từ thư mục jobs
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "jobs")))
from split_pdf import split_and_upload_batch_from_minio  # type: ignore


def _run_pdf_split() -> None:
    # Execute split stage for all source PDFs in MinIO / Chạy bước tách trang cho toàn bộ PDF nguồn trên MinIO
    split_and_upload_batch_from_minio()


default_args = {
    "owner": "hvb",
    "depends_on_past": False,
    "start_date": datetime(2026, 6, 11),
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    "hvb_pdf_split_pipeline",
    default_args=default_args,
    description="Split input PDFs into single-page PDFs and upload pages to MinIO",
    schedule_interval=None,
    catchup=False,
    max_active_runs=1,
    tags=["hvb", "pdf", "split"],
) as dag:
    split_pdf_task = PythonOperator(
        task_id="split_pdf_and_upload",
        python_callable=_run_pdf_split,
    )
