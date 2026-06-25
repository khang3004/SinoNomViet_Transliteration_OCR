from __future__ import annotations

import os
import sys

# pyrefly: ignore [missing-import]
from airflow import DAG  # noqa: F401 — required for dag_discovery_safe_mode / Bắt buộc để Airflow nhận file DAG

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "jobs")))
from pipeline_common import create_ocr_model_dag  # type: ignore

dag = create_ocr_model_dag("chatgpt", "ChatGPT Vision")
