from __future__ import annotations

import json
from pathlib import Path

from common.io_storage import download_object, list_objects_with_prefix


def parse_pages_filter(raw: str | list[int] | None) -> set[int] | None:
    # Parse page filter like "1,3,5" or "1-10" / Parse lọc trang dạng "1,3,5" hoặc "1-10"
    if raw is None:
        return None
    if isinstance(raw, list):
        return {int(page) for page in raw if str(page).strip()}
    text = str(raw).strip()
    if not text:
        return None

    pages: set[int] = set()
    for part in text.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text.strip())
            end = int(end_text.strip())
            if end < start:
                raise ValueError(f"Invalid page range '{token}': end < start")
            pages.update(range(start, end + 1))
        else:
            pages.add(int(token))
    return pages or None


def load_manifest(bucket_raw: str, manifest_key: str) -> dict:
    # Download manifest JSON from MinIO / Tải manifest JSON từ MinIO
    local_manifest = download_object(
        bucket=bucket_raw,
        object_name=manifest_key,
        local_path=Path("/tmp") / "hvb_manifests" / Path(manifest_key).name,
    )
    try:
        return json.loads(local_manifest.read_text(encoding="utf-8"))
    finally:
        if local_manifest.exists():
            local_manifest.unlink(missing_ok=True)


def resolve_manifest_for_doc(
    bucket_raw: str,
    manifest_prefix: str,
    doc_id: str,
) -> tuple[str, dict]:
    # Find split manifest for one document / Tìm manifest split cho một tài liệu
    manifest_keys = list_objects_with_prefix(bucket=bucket_raw, prefix=manifest_prefix, suffix=".json")
    for manifest_key in manifest_keys:
        manifest = load_manifest(bucket_raw=bucket_raw, manifest_key=manifest_key)
        if manifest.get("doc_id") == doc_id:
            return manifest_key, manifest
    raise ValueError(f"No manifest found for doc_id='{doc_id}' under prefix '{manifest_prefix}'")


# Backward-compatible aliases used by older call sites / Alias tương thích ngược
_resolve_manifest_for_doc = resolve_manifest_for_doc
