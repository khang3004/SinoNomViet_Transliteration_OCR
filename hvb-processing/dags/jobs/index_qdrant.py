from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from common.config import get_value, load_config
from common.embeddings import embed_texts
from common.io_storage import download_object, list_objects_with_prefix
from common.qdrant_schema import ensure_hvb_database_collection, get_qdrant_client, get_qdrant_settings
from ocr_runner import parse_pages_filter

PAGE_OBJECT_PATTERN = re.compile(r"page_(\d+)\.json$")


def _ocr_pages_prefix(model_folder: str, doc_id: str) -> str:
    # Build MinIO prefix for per-page OCR JSON files / Tạo prefix MinIO cho JSON OCR từng trang
    cfg = load_config()
    ocr_prefix = get_value(cfg, "minio", "ocr_output_prefix", fallback="ocr").strip("/")
    return f"{ocr_prefix}/{model_folder}/{doc_id}/"


def _parse_page_no_from_key(object_key: str) -> int | None:
    # Extract page number from page_0001.json object key / Lấy số trang từ object key
    match = PAGE_OBJECT_PATTERN.search(Path(object_key).name)
    if not match:
        return None
    return int(match.group(1))


def _make_point_id(
    doc_id: str,
    model_name: str,
    page_no: int,
    *,
    chunk_type: str,
    block_index: int | None = None,
) -> str:
    # Deterministic point id for idempotent upsert / ID point cố định để upsert idempotent
    block_part = "page" if block_index is None else str(block_index)
    raw = f"{doc_id}|{model_name}|{page_no}|{chunk_type}|{block_part}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _load_page_payload(bucket: str, object_key: str) -> dict[str, Any]:
    # Download and parse one page OCR JSON / Tải và parse JSON OCR một trang
    local_path = download_object(
        bucket=bucket,
        object_name=object_key,
        local_path=Path("/tmp") / "hvb_index" / bucket / object_key,
    )
    try:
        data = json.loads(local_path.read_text(encoding="utf-8"))
    finally:
        if local_path.exists():
            local_path.unlink(missing_ok=True)
    if not isinstance(data, dict):
        raise ValueError(f"Expected object JSON in {object_key}, got {type(data).__name__}")
    return data


def _build_chunks(
    payload: dict[str, Any],
    *,
    chunk_mode: str,
    minio_bucket: str,
    minio_key: str,
) -> list[dict[str, Any]]:
    # Build index chunks from OCR page payload / Tạo chunk index từ payload OCR một trang
    doc_id = str(payload.get("doc_id", ""))
    page_no = int(payload.get("page_no", 0))
    model_name = str(payload.get("model_name", ""))
    base = {
        "doc_id": doc_id,
        "page_no": page_no,
        "model_name": model_name,
        "source_pdf": str(payload.get("source_pdf", "")),
        "minio_bucket": minio_bucket,
        "minio_key": minio_key,
        "page_confidence": float(payload.get("confidence", 0.0)),
        "created_at": str(payload.get("created_at", "")),
    }

    if chunk_mode == "page":
        text = str(payload.get("text", "")).strip()
        if not text:
            return []
        return [
            {
                **base,
                "chunk_type": "page",
                "text": text,
                "confidence": float(payload.get("confidence", 0.0)),
            }
        ]

    if chunk_mode == "block":
        chunks: list[dict[str, Any]] = []
        blocks = payload.get("blocks") or []
        if not isinstance(blocks, list):
            return chunks
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = str(block.get("text", "")).strip()
            if not text:
                continue
            chunks.append(
                {
                    **base,
                    "chunk_type": "block",
                    "block_index": index,
                    "text": text,
                    "confidence": float(block.get("confidence", payload.get("confidence", 0.0))),
                    "box": block.get("box"),
                }
            )
        return chunks

    raise ValueError(f"Unsupported chunk_mode '{chunk_mode}'. Use: page, block")


def index_page_object(
    bucket: str,
    object_key: str,
    *,
    chunk_mode: str = "page",
    force_reindex: bool = False,
) -> int:
    # Index one MinIO page JSON into Qdrant / Index một JSON trang từ MinIO vào Qdrant
    del force_reindex  # Upsert is idempotent; reserved for future skip logic / Upsert đã idempotent
    payload = _load_page_payload(bucket, object_key)
    if payload.get("error"):
        print(f"[index] skip {object_key}: OCR error={payload.get('error')}")
        return 0

    chunks = _build_chunks(payload, chunk_mode=chunk_mode, minio_bucket=bucket, minio_key=object_key)
    if not chunks:
        print(f"[index] skip {object_key}: no text chunks")
        return 0

    vectors = embed_texts([chunk["text"] for chunk in chunks])
    settings = get_qdrant_settings()
    if vectors and len(vectors[0]) != settings["vector_size"]:
        raise ValueError(
            f"Embedding dimension {len(vectors[0])} != qdrant.vector_size {settings['vector_size']}"
        )

    from qdrant_client.models import PointStruct

    points: list[PointStruct] = []
    for chunk, vector in zip(chunks, vectors):
        point_id = _make_point_id(
            chunk["doc_id"],
            chunk["model_name"],
            int(chunk["page_no"]),
            chunk_type=str(chunk["chunk_type"]),
            block_index=chunk.get("block_index"),
        )
        payload = {key: value for key, value in chunk.items() if value is not None}
        points.append(PointStruct(id=point_id, vector=vector, payload=payload))

    client = get_qdrant_client()
    client.upsert(collection_name=settings["collection"], points=points)
    print(f"[index] upserted {len(points)} point(s) from {object_key}")
    return len(points)


def index_pages_from_minio(
    *,
    doc_id: str,
    model_folder: str = "paddle",
    pages: str | list[int] | None = None,
    chunk_mode: str = "page",
    force_reindex: bool = False,
) -> dict[str, int]:
    # Loop page JSON objects under MinIO prefix and index each / Lặp JSON trang trên MinIO và index từng file
    cfg = load_config()
    bucket = get_value(cfg, "minio", "bucket_output")
    page_filter = parse_pages_filter(pages)
    prefix = _ocr_pages_prefix(model_folder, doc_id)

    ensure_hvb_database_collection()

    object_keys = [
        key
        for key in list_objects_with_prefix(bucket=bucket, prefix=prefix, suffix=".json")
        if Path(key).name.startswith("page_")
    ]

    indexed_pages = 0
    indexed_points = 0
    for object_key in object_keys:
        page_no = _parse_page_no_from_key(object_key)
        if page_no is None:
            continue
        if page_filter is not None and page_no not in page_filter:
            continue
        point_count = index_page_object(
            bucket,
            object_key,
            chunk_mode=chunk_mode,
            force_reindex=force_reindex,
        )
        if point_count > 0:
            indexed_pages += 1
            indexed_points += point_count

    summary = {
        "doc_id": doc_id,
        "model_folder": model_folder,
        "indexed_pages": indexed_pages,
        "indexed_points": indexed_points,
        "chunk_mode": chunk_mode,
    }
    print(f"[index] done: {summary}")
    return summary
