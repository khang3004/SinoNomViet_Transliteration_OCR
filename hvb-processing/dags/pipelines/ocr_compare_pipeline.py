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

# Allow importing from jobs folder / Cho phép import từ thư mục jobs
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "jobs")))
from ocr_runner import SUPPORTED_MODELS, run_compare_from_split_minio  # type: ignore

DEFAULT_COMPARE_MODELS = list(SUPPORTED_MODELS)


def _parse_models(value: str | list[str]) -> list[str]:
    # Parse models from comma-separated string or list / Parse danh sách model từ chuỗi hoặc list
    if isinstance(value, list):
        return [item.strip() for item in value if item.strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _optional_pages(value: object | None) -> str | None:
    # Normalize optional pages filter / Chuẩn hóa filter trang; None = tất cả
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _run_ocr_compare(**context) -> None:
    # Compare multiple OCR models on same dataset / So sánh nhiều model OCR trên cùng dataset
    params = context.get("params", {})
    dag_run = context.get("dag_run")
    models = _parse_models(params.get("models", ",".join(DEFAULT_COMPARE_MODELS)))
    upload_minio = params.get("upload_minio", True)
    doc_id = params.get("doc_id", "")
    pages = _optional_pages(params.get("pages"))
    if dag_run and dag_run.conf:
        if "models" in dag_run.conf:
            models = _parse_models(dag_run.conf["models"])
        upload_minio = dag_run.conf.get("upload_minio", upload_minio)
        doc_id = dag_run.conf.get("doc_id", doc_id)
        pages = _optional_pages(dag_run.conf.get("pages", pages))
    run_compare_from_split_minio(
        models=models,
        upload_minio=upload_minio,
        doc_id=doc_id or None,
        pages=pages,
    )


default_args = {
    "owner": "hvb",
    "depends_on_past": False,
    "start_date": datetime(2026, 6, 11),
    "retries": 0,
}

with DAG(
    "hvb_ocr_compare_pipeline",
    default_args=default_args,
    description="Run multiple OCR models on split pages from MinIO manifests",
    schedule_interval=None,
    catchup=False,
    max_active_runs=1,
    tags=["hvb", "ocr", "compare"],
    params={
        "models": Param(
            default=",".join(DEFAULT_COMPARE_MODELS),
            type="string",
            description="Danh sách model, cách nhau bởi dấu phẩy (vd: paddle,gemini,google_vision)",
        ),
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
            default=None,
            type=["null", "string"],
            description="(Tùy chọn) Trang: 1 | 1,3,5 | 1-10. Không điền = tất cả trang.",
        ),
    },
) as dag:
    ocr_compare_task = PythonOperator(
        task_id="run_ocr_compare",
        python_callable=_run_ocr_compare,
    )
