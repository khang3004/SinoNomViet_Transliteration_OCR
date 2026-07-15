from __future__ import annotations

from typing import Any

from common.config import get_value, load_config

DEFAULT_COLLECTION_PAIRS = "hvb_chau_ban_pairs"
DEFAULT_VECTOR_SIZE = 384
DEFAULT_DISTANCE = "cosine"

PAIRS_PAYLOAD_INDEX_FIELDS: dict[str, str] = {
    "doc_id": "keyword",
    "page_no": "integer",
    "printed_page": "integer",
    "pair_id": "keyword",
    "pair_index": "integer",
    "entry_id": "keyword",
    "stt": "integer",
    "tap_id": "keyword",
    "trieu_dai": "keyword",
    "tap_so": "integer",
    "tap_label": "keyword",
    "the_loai": "keyword",
    "page_type": "keyword",
    "status": "keyword",
    "ocr_model": "keyword",
    "align_model": "keyword",
    "pipeline_version": "keyword",
}


def _distance_from_config(raw: str) -> Any:
    # Map config string to Qdrant distance enum / Map chuỗi config sang enum distance của Qdrant
    from qdrant_client.models import Distance

    mapping = {
        "cosine": Distance.COSINE,
        "euclid": Distance.EUCLID,
        "euclidean": Distance.EUCLID,
        "dot": Distance.DOT,
    }
    key = raw.strip().lower()
    if key not in mapping:
        raise ValueError(f"Unsupported qdrant.distance '{raw}'. Use: cosine, euclid, dot")
    return mapping[key]


def get_qdrant_settings() -> dict[str, Any]:
    # Load Qdrant connection settings from config / Đọc cấu hình kết nối Qdrant từ config
    cfg = load_config()
    return {
        "url": get_value(
            cfg,
            "qdrant",
            "url",
            fallback="http://qdrant-nodeport.qdrant.svc.cluster.local:6333",
        ),
        "api_key": get_value(cfg, "qdrant", "api_key", fallback="") or None,
        "collection_pairs": get_value(
            cfg, "qdrant", "collection_pairs", fallback=DEFAULT_COLLECTION_PAIRS
        ),
        "vector_size": int(get_value(cfg, "qdrant", "vector_size", fallback=str(DEFAULT_VECTOR_SIZE))),
        "distance": get_value(cfg, "qdrant", "distance", fallback=DEFAULT_DISTANCE),
    }


def get_qdrant_client():
    # Build Qdrant client from config / Tạo Qdrant client từ config
    try:
        from qdrant_client import QdrantClient
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'qdrant-client'. Install dags/requirements.txt.") from exc

    settings = get_qdrant_settings()
    return QdrantClient(url=settings["url"], api_key=settings["api_key"])


def ensure_hvb_chau_ban_pairs_collection(*, recreate: bool = False) -> str:
    # Create 1-point-per-pair Châu bản collection / Tạo collection 1 point / cặp Châu bản
    from qdrant_client.models import PayloadSchemaType, VectorParams

    settings = get_qdrant_settings()
    collection = settings["collection_pairs"]
    client = get_qdrant_client()

    if recreate and client.collection_exists(collection):
        client.delete_collection(collection)

    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(
                size=settings["vector_size"],
                distance=_distance_from_config(settings["distance"]),
            ),
        )

    schema_map = {
        "keyword": PayloadSchemaType.KEYWORD,
        "integer": PayloadSchemaType.INTEGER,
        "bool": PayloadSchemaType.BOOL,
    }
    for field_name, field_type in PAIRS_PAYLOAD_INDEX_FIELDS.items():
        client.create_payload_index(
            collection_name=collection,
            field_name=field_name,
            field_schema=schema_map[field_type],
        )
    return collection


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Initialize HVB Qdrant pairs collection")
    parser.add_argument("--recreate", action="store_true", help="Drop and recreate collection")
    args = parser.parse_args()
    name = ensure_hvb_chau_ban_pairs_collection(recreate=args.recreate)
    print(f"Qdrant collection ready: {name}")
