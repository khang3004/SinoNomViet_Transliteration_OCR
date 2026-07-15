from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.chau_ban_schema import make_entry_pair_id, make_pair_point_id
from common.config import get_value, load_config
from common.embeddings import embed_texts
from common.entry_quality import effective_confidence, should_index_entry, source_page_nos
from common.io_storage import download_object, list_objects_with_prefix, v2_catalog_key
from common.page_utils import parse_pages_filter
from common.qdrant_schema import ensure_hvb_chau_ban_pairs_collection, get_qdrant_client, get_qdrant_settings


def _load_json(bucket: str, object_key: str) -> dict[str, Any]:
    local_path = download_object(
        bucket=bucket,
        object_name=object_key,
        local_path=Path("/tmp") / "hvb_index_catalog" / bucket / object_key,
    )
    try:
        data = json.loads(local_path.read_text(encoding="utf-8"))
    finally:
        if local_path.exists():
            local_path.unlink(missing_ok=True)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {bucket}/{object_key}")
    return data


def index_catalog_entries_to_qdrant(
    *,
    doc_id: str,
    pages: str | list[int] | None = None,
    recreate: bool = False,
) -> dict[str, int]:
    """Index catalog/entry trich_yeu as 1 Qdrant point per STT pair.

    Index trích yếu catalog: 1 point Qdrant / cặp theo STT.
    """
    from qdrant_client.models import PointStruct

    cfg = load_config()
    bucket_entries = get_value(cfg, "minio", "bucket_entries", fallback="hvb-entries")
    bucket_catalog = get_value(cfg, "minio", "bucket_catalog", fallback="hvb-catalog")
    page_filter = parse_pages_filter(pages)

    ensure_hvb_chau_ban_pairs_collection(recreate=recreate)

    # Prefer per-STT entry files; fall back to catalog list / Ưu tiên file entry theo STT
    entry_keys = [
        key
        for key in list_objects_with_prefix(bucket=bucket_entries, prefix=f"{doc_id}/", suffix=".json")
        if Path(key).name.startswith("stt_")
    ]
    if not entry_keys:
        catalog = _load_json(bucket_catalog, v2_catalog_key(doc_id))
        print(f"[index_catalog] no entry files; catalog has {catalog.get('entry_count')} rows (need entries)")
        return {"indexed_entries": 0, "pair_points": 0}

    chunks: list[dict[str, Any]] = []
    skipped = 0
    min_conf = float(get_value(cfg, "pipeline", "index_min_confidence", fallback="0.45"))
    for object_key in entry_keys:
        entry = _load_json(bucket_entries, object_key)
        source_pages = entry.get("source_pages") or []
        if page_filter is not None:
            page_nos = {
                int(row.get("page_no"))
                for row in source_pages
                if isinstance(row, dict) and row.get("page_no") is not None
            }
            if page_nos and page_nos.isdisjoint(page_filter):
                continue

        if not should_index_entry(entry, min_confidence=min_conf):
            skipped += 1
            print(
                f"[index_catalog] skip STT={entry.get('stt')} "
                f"status={entry.get('status')} flags={entry.get('flags')}"
            )
            continue

        chi_muc = entry.get("chi_muc") or {}
        pairs = entry.get("content_alignment") or []
        if not pairs:
            trich = entry.get("trich_yeu") or {}
            han_nom = str(trich.get("han_nom", "")).strip()
            quoc_ngu = str(trich.get("quoc_ngu", "")).strip()
            if han_nom.lower() in {"none", "null"}:
                han_nom = ""
            if quoc_ngu.lower() in {"none", "null"}:
                quoc_ngu = ""
            de_tai = str(chi_muc.get("de_tai") or "").strip()
            if de_tai.lower() in {"none", "null"}:
                de_tai = ""
            # Always stable pair_id for upsert overwrite / pair_id ổn định để upsert ghi đè
            entry_id = str(entry.get("entry_id") or "")
            stable_pair = make_entry_pair_id(entry_id, 0) if entry_id else ""
            if han_nom or quoc_ngu:
                pairs = [
                    {
                        "pair_index": 0,
                        "pair_id": stable_pair or entry_id,
                        "han_nom": han_nom,
                        "quoc_ngu": quoc_ngu,
                    }
                ]
            elif de_tai:
                # Catalog-only stub: đề tài as searchable text /
                # Stub chỉ mục: dùng đề tài làm text tìm kiếm
                pairs = [
                    {
                        "pair_index": 0,
                        "pair_id": stable_pair or entry_id,
                        "han_nom": "",
                        "quoc_ngu": de_tai,
                        "source_kind": "de_tai_stub",
                    }
                ]

        best_conf = effective_confidence(entry)
        page_nos = source_page_nos(entry)
        printed_pages = []
        for row in source_pages:
            if isinstance(row, dict) and row.get("printed_page") is not None:
                try:
                    printed_pages.append(int(row["printed_page"]))
                except (TypeError, ValueError):
                    pass
        printed_page = printed_pages[0] if printed_pages else None
        page_no = page_nos[0] if page_nos else None

        entry_id_str = str(entry.get("entry_id") or "")
        tap = entry.get("tap") if isinstance(entry.get("tap"), dict) else {}
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            # Prefer make_entry_pair_id so re-index overwrites same point /
            # Ưu tiên make_entry_pair_id để re-index ghi đè cùng point
            try:
                pair_index = int(pair.get("pair_index", 0))
            except (TypeError, ValueError):
                pair_index = 0
            pair_id = (
                make_entry_pair_id(entry_id_str, pair_index)
                if entry_id_str
                else str(pair.get("pair_id") or "")
            )
            han_nom = str(pair.get("han_nom", "")).strip()
            quoc_ngu = str(pair.get("quoc_ngu", "")).strip()
            if not pair_id or (not han_nom and not quoc_ngu):
                continue
            if han_nom.lower() == "none" or quoc_ngu.lower() == "none":
                continue
            de_tai = chi_muc.get("de_tai")
            embed_text = quoc_ngu or han_nom
            if de_tai and quoc_ngu:
                embed_text = f"{de_tai}. {quoc_ngu}"
            chunks.append(
                {
                    "doc_id": doc_id,
                    "entry_id": entry.get("entry_id"),
                    "stt": entry.get("stt"),
                    "tap_id": tap.get("tap_id"),
                    "trieu_dai": tap.get("trieu_dai"),
                    "tap_so": tap.get("tap_so"),
                    "tap_label": tap.get("label"),
                    "page_no": page_no,
                    "page_nos": page_nos,
                    "printed_page": printed_page,
                    "printed_pages": printed_pages or None,
                    "page_type": "muc_luc",
                    "pair_index": int(pair.get("pair_index", 0)),
                    "pair_id": pair_id,
                    "ngay_thang": chi_muc.get("ngay_thang"),
                    "to_tap": chi_muc.get("to_tap"),
                    "the_loai": chi_muc.get("the_loai"),
                    "xuat_xu": chi_muc.get("xuat_xu"),
                    "de_tai": de_tai,
                    "han_nom": han_nom,
                    "quoc_ngu": quoc_ngu,
                    "text": embed_text,
                    "status": entry.get("status"),
                    "flags": entry.get("flags"),
                    "ocr_confidence": entry.get("ocr_confidence"),
                    "refine_confidence": entry.get("refine_confidence"),
                    "stitch_confidence": entry.get("stitch_confidence"),
                    "confidence": best_conf,
                    "pipeline_version": entry.get("pipeline_version"),
                    "entry_minio_key": f"{bucket_entries}/{object_key}",
                }
            )

    if not chunks:
        print(f"[index_catalog] nothing to index (skipped={skipped})")
        return {"indexed_entries": 0, "pair_points": 0, "skipped": skipped}

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
    totals = {
        "indexed_entries": len({c.get("entry_id") for c in chunks}),
        "pair_points": len(points),
        "skipped": skipped,
    }
    print(f"[index_catalog] done: {totals}")
    return totals
