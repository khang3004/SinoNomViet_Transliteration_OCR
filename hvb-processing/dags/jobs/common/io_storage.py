from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

# Ensure `common` package is importable when Airflow parses files directly / Đảm bảo import được `common` khi Airflow parse trực tiếp file
JOBS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if JOBS_DIR not in sys.path:
    sys.path.append(JOBS_DIR)

from common.config import get_value, load_config

if TYPE_CHECKING:
    from minio import Minio


def _parse_minio_endpoint(endpoint: str) -> tuple[str, bool]:
    # Support endpoint with or without scheme / Hỗ trợ endpoint có hoặc không có scheme
    if "://" in endpoint:
        parsed = urlparse(endpoint)
        host = parsed.netloc or parsed.path
        secure = parsed.scheme == "https"
        return host, secure
    return endpoint, False


def get_minio_client(
    endpoint: str | None = None,
    access_key: str | None = None,
    secret_key: str | None = None,
    secure: bool | None = None,
) -> "Minio":
    # Create reusable MinIO client from config / Tạo MinIO client từ config
    try:
        # Import lazily to avoid breaking DAG parsing / Import trễ để tránh vỡ DAG lúc parse
        from minio import Minio
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'minio'. Install it in Airflow runtime before uploading artifacts."
        ) from exc

    cfg = load_config()
    raw_endpoint = endpoint or get_value(cfg, "minio", "endpoint")
    host, parsed_secure = _parse_minio_endpoint(raw_endpoint)
    return Minio(
        host,
        access_key=access_key or get_value(cfg, "minio", "access_key"),
        secret_key=secret_key or get_value(cfg, "minio", "secret_key"),
        secure=parsed_secure if secure is None else secure,
    )


def upload_file(client: "Minio", bucket: str, object_name: str, local_path: Path) -> None:
    # Upload generated artifact to object storage / Upload output lên object storage
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    client.fput_object(bucket_name=bucket, object_name=object_name, file_path=str(local_path))


def ensure_bucket(client: "Minio", bucket: str) -> None:
    # Create bucket only when missing / Chỉ tạo bucket khi chưa tồn tại
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def upload_output_dir(output_dir: str) -> list[str]:
    # Upload all JSON outputs to configured MinIO bucket / Upload toàn bộ JSON output lên MinIO
    cfg = load_config()
    bucket = get_value(cfg, "minio", "bucket_output")
    client = get_minio_client()
    # Ensure target bucket once before batch upload / Đảm bảo bucket đích một lần trước khi upload hàng loạt
    ensure_bucket(client, bucket)
    local_root = Path(output_dir)
    if not local_root.exists():
        print(f"[minio] skip upload_output_dir: missing local dir {local_root}")
        return []
    uploaded: list[str] = []
    for file_path in sorted(local_root.glob("*.json")):
        object_name = f"ocr/{file_path.name}"
        client.fput_object(bucket_name=bucket, object_name=object_name, file_path=str(file_path))
        uploaded.append(f"{bucket}/{object_name}")
    return uploaded


def upload_files_with_prefix(
    bucket: str, local_dir: Path, object_prefix: str, glob_pattern: str
) -> list[str]:
    # Upload all files matching pattern under a MinIO prefix / Upload tất cả file theo pattern lên MinIO prefix
    client = get_minio_client()
    # Ensure target bucket once before prefix upload / Đảm bảo bucket đích một lần trước khi upload theo prefix
    ensure_bucket(client, bucket)
    uploaded: list[str] = []
    for file_path in sorted(local_dir.glob(glob_pattern)):
        object_name = f"{object_prefix}/{file_path.name}"
        client.fput_object(bucket_name=bucket, object_name=object_name, file_path=str(file_path))
        uploaded.append(f"{bucket}/{object_name}")
    return uploaded


def ocr_page_object_key(model_folder: str, doc_id: str, page_no: int, prefix: str = "ocr") -> str:
    # Build MinIO key for one OCR page JSON / Tạo object key MinIO cho JSON một trang OCR
    normalized_prefix = prefix.strip("/")
    return f"{normalized_prefix}/{model_folder}/{doc_id}/page_{page_no:04d}.json"


def upload_page_ocr_result(
    result_payload: dict,
    *,
    model_folder: str,
    doc_id: str,
    page_no: int,
    bucket: str | None = None,
    prefix: str | None = None,
) -> str:
    # Upload single-page OCR JSON to model-specific folder / Upload JSON OCR một trang vào folder model
    cfg = load_config()
    target_bucket = bucket or get_value(cfg, "minio", "bucket_output")
    ocr_prefix = prefix or get_value(cfg, "minio", "ocr_output_prefix", fallback="ocr")
    object_name = ocr_page_object_key(model_folder, doc_id, page_no, prefix=ocr_prefix)
    return upload_json_payload(target_bucket, object_name, result_payload)


def upload_json_payload(bucket: str, object_name: str, payload: dict) -> str:
    # Upload in-memory JSON payload via temp file / Upload JSON trong bộ nhớ thông qua file tạm
    temp_path = Path("/tmp") / f"{Path(object_name).name}.tmp.json"
    temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        client = get_minio_client()
        upload_file(client, bucket, object_name, temp_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return f"{bucket}/{object_name}"


def list_objects_with_prefix(bucket: str, prefix: str, suffix: str | None = None) -> list[str]:
    # List object keys under a prefix with optional suffix filter / Liệt kê object theo prefix, có thể lọc đuôi file
    client = get_minio_client()
    ensure_bucket(client, bucket)
    keys: list[str] = []
    for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
        if obj.is_dir:
            continue
        if suffix and not obj.object_name.endswith(suffix):
            continue
        keys.append(obj.object_name)
    return sorted(keys)


def download_object(bucket: str, object_name: str, local_path: Path) -> Path:
    # Download one object to local filesystem / Tải một object từ MinIO về local
    client = get_minio_client()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    client.fget_object(bucket_name=bucket, object_name=object_name, file_path=str(local_path))
    return local_path
