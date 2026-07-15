from __future__ import annotations

import os

from common.config import get_value, load_config
from common.io_storage import object_exists


def force_reprocess() -> bool:
    # Force overwrite even when MinIO artifact exists / Ép chạy lại kể cả khi đã có artifact MinIO
    cfg = load_config()
    env = os.environ.get("HVB_FORCE", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    raw = get_value(cfg, "pipeline", "force_reprocess", fallback="false")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def should_skip_api_call(bucket: str, object_name: str) -> bool:
    """Skip paid API when artifact already stored; honor HVB_FORCE.

    Bỏ qua gọi API trả phí nếu artifact đã có trên MinIO; vẫn chạy khi HVB_FORCE.
    """
    if force_reprocess():
        return False
    exists = object_exists(bucket, object_name)
    if exists:
        print(f"[state] skip API — artifact exists: {bucket}/{object_name}")
    return exists
