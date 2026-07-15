from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common.chau_ban_schema import make_pair_point_id
from common.config import get_value, load_config
from common.embeddings import embed_texts
from common.io_storage import download_object, list_objects_with_prefix
from common.page_utils import parse_pages_filter
from common.qdrant_schema import ensure_hvb_chau_ban_pairs_collection, get_qdrant_client, get_qdrant_settings

PAGE_OBJECT_PATTERN = re.compile(r"page_(\d+)\.json$")


def _load_aligned(bucket: str, object_key: str) -> dict[str, Any]:
    local_path = download_object(
        bucket=bucket,
        object_name=object_key,
        local_path=Path("/tmp") / "hvb_index_pairs" / bucket / object_key,
    )
    try:
        data = json.loads(local_path.read_text(encoding="utf-8"))
    finally:
        if local_path.exists():
            local_path.unlink(missing_ok=True)
    if not isinstance(data, dict):
        raise ValueError(f"Expected aligned JSON in {object_key}")
    return data


def index_aligned_page_pairs(bucket: str, object_key: str) -> int:
    # Index one aligned page: 1 Qdrant point per pair / Index một trang align: 1 point / cặp
    payload = _load_aligned(bucket, object_key)
    pairs = payload.get("content_alignment") or []
    if not isinstance(pairs, list) or not pairs:
        print(f"[index_pairs] skip {object_key}: no pairs")
        return 0

    chunks: list[dict[str, Any]] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        pair_id = str(pair.get("pair_id") or "")
        han_nom = str(pair.get("han_nom", "")).strip()
        quoc_ngu = str(pair.get("quoc_ngu", "")).strip()
        if not pair_id or (not han_nom and not quoc_ngu):
            continue
        # Embed quốc ngữ preferentially for Vietnamese search / Ưu tiên embed quốc ngữ để search tiếng Việt
        embed_text = quoc_ngu or han_nom
        de_tai = payload.get("de_tai")
        if de_tai and quoc_ngu:
            embed_text = f"{de_tai}. {quoc_ngu}"
        chunks.append(
            {
                "doc_id": str(payload.get("doc_id", "")),
                "page_no": int(payload.get("page_no", 0)),
                "pair_index": int(pair.get("pair_index", 0)),
                "pair_id": pair_id,
                "ngay_thang": payload.get("ngay_thang"),
                "the_loai": payload.get("the_loai"),
                "de_tai": payload.get("de_tai"),
                "page_header": payload.get("page_header"),
                "printed_page": payload.get("printed_page"),
                "page_type": payload.get("page_type") or "body",
                "han_nom": han_nom,
                "quoc_ngu": quoc_ngu,
                "text": embed_text,
                "ocr_model": payload.get("ocr_model"),
                "ocr_provider": payload.get("ocr_provider"),
                "ocr_at": payload.get("ocr_at"),
                "align_model": payload.get("align_model"),
                "align_at": payload.get("align_at"),
                "preprocess_version": payload.get("preprocess_version"),
                "pipeline_version": payload.get("pipeline_version"),
                "source_png_key": payload.get("source_png_key"),
                "aligned_minio_key": f"{bucket}/{object_key}",
            }
        )

    if not chunks:
        return 0

    from qdrant_client.models import PointStruct

    settings = get_qdrant_settings()
    vectors = embed_texts([chunk["text"] for chunk in chunks])
    points = [
        PointStruct(
            id=make_pair_point_id(chunk["pair_id"]),
            vector=vector,
            payload={key: value for key, value in chunk.items() if value is not None},
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    client = get_qdrant_client()
    client.upsert(collection_name=settings["collection_pairs"], points=points)
    print(f"[index_pairs] {object_key}: {len(points)} pair point(s)")
    return len(points)


def index_aligned_pairs_from_minio(
    *,
    doc_id: str,
    pages: str | list[int] | None = None,
) -> dict[str, int]:
    # Loop hvb-aligned pages into Qdrant pairs collection / Lặp trang align vào collection pairs
    cfg = load_config()
    bucket = get_value(cfg, "minio", "bucket_aligned", fallback="hvb-aligned")
    page_filter = parse_pages_filter(pages)
    ensure_hvb_chau_ban_pairs_collection()

    object_keys = [
        key
        for key in list_objects_with_prefix(bucket=bucket, prefix=f"{doc_id}/", suffix=".json")
        if Path(key).name.startswith("page_")
    ]

    totals = {"indexed_pages": 0, "pair_points": 0}
    for object_key in object_keys:
        match = PAGE_OBJECT_PATTERN.search(Path(object_key).name)
        if not match:
            continue
        page_no = int(match.group(1))
        if page_filter is not None and page_no not in page_filter:
            continue
        count = index_aligned_page_pairs(bucket, object_key)
        if count:
            totals["indexed_pages"] += 1
            totals["pair_points"] += count

    print(f"[index_pairs] done: {totals}")
    return totals
