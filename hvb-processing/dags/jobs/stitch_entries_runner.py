from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.chau_ban_schema import (
    PIPELINE_VERSION,
    extract_json_object,
    make_entry_id,
    make_entry_pair_id,
    utc_now_iso,
)
from common.config import get_value, load_config
from common.entry_quality import (
    CONFIDENCE_METHOD,
    clean_trich_yeu,
    derive_entry_status,
    detect_entry_flags,
    estimate_entry_ocr_confidence,
    is_cut_at_trich_yeu_label,
)
from common.io_storage import (
    download_object,
    entry_object_key,
    list_objects_with_prefix,
    object_exists,
    upload_json_payload,
    v2_ocr_page_key,
)
from common.llm_chat import call_chat_completion
from common.page_utils import parse_pages_filter
from common.tap_index import is_blank_page, is_tap_parent_page, tap_payload_for_page

_STITCH_PROMPT = """Bạn là chuyên gia nối văn bản Châu bản bị cắt trang (mục lục).
ENTRY hiện tại có thể:
- chỉ còn "N." + một phần metadata (Ngày và/hoặc Tờ/Tập/Loại...) rồi hết trang — phần còn lại + TY ở entry_continuation trang kế (không số STT mới), hoặc
- chỉ còn nhãn TRÍCH YẾU (trich_yeu rỗng) — body nằm orphan_head trang kế.

CONTEXT là OCR trang kế (entry_continuation + orphan_head + entries).

Nhiệm vụ:
1) Ưu tiên entry_continuation của trang kế (metadata + trich_yeu không số STT) — merge vào ENTRY.
2) Nếu không: dùng orphan_head (chỉ body TY).
3) Nếu không: tìm đoạn đầu trang kế TRƯỚC số STT đầu tiên.
4) Giữ \\n. KHÔNG bịa. KHÔNG gộp sang STT khác trong entries[] trang kế.
5) append_source_page = số trang kế khi nối được.
6) Trả trich_yeu ĐẦY ĐỦ sau khi nối (gồm phần cũ + mới nếu có).

Trả về ĐÚNG một JSON:
{{
  "stitched": true,
  "chi_muc": {{"ngay_thang": "...", "to_tap": "...", "the_loai": "...", "xuat_xu": "...", "de_tai": "..."}},
  "trich_yeu": {{"han_nom": "...", "quoc_ngu": "..."}},
  "append_source_page": số_trang_hoặc_null,
  "stitch_confidence": 0.0,
  "notes": "ngắn"
}}

ENTRY:
{entry_json}

NEXT_PAGE_OCR:
{next_page_json}
"""


def _load_json(bucket: str, object_key: str) -> dict[str, Any]:
    local_path = download_object(
        bucket=bucket,
        object_name=object_key,
        local_path=Path("/tmp") / "hvb_stitch" / bucket / object_key,
    )
    try:
        data = json.loads(local_path.read_text(encoding="utf-8"))
    finally:
        if local_path.exists():
            local_path.unlink(missing_ok=True)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {bucket}/{object_key}")
    return data


def _needs_stitch(entry: dict[str, Any]) -> bool:
    flags = set(entry.get("flags") or [])
    if "needs_stitch" in flags or "cut_at_trich_yeu_label" in flags:
        return True
    if "empty_trich_yeu" in flags:
        return True
    if is_cut_at_trich_yeu_label(
        trich_yeu=entry.get("trich_yeu"),
        is_last_on_page=any(
            isinstance(row, dict) and bool(row.get("is_last_on_page"))
            for row in (entry.get("source_pages") or [])
        ),
        flags=list(flags),
    ):
        return True
    return any(f.startswith("truncated_") for f in flags)


def _mark_pending_continuation(
    entry: dict[str, Any],
    *,
    next_page: int,
    notes: str,
    upload_minio: bool,
    bucket_entries: str,
    doc_id: str,
) -> None:
    """Keep needs_stitch when label-cut and continuation not found yet.

    Giữ needs_stitch khi cắt ở nhãn và chưa tìm được continuation.
    """
    flags = set(entry.get("flags") or [])
    flags.add("needs_stitch")
    flags.add("cut_at_trich_yeu_label")
    flags.add("pending_continuation")
    entry["flags"] = sorted(flags)
    entry["status"] = "needs_stitch"
    entry["stitch_notes"] = notes
    entry["stitch_pending_next_page"] = int(next_page)
    entry["updated_at"] = utc_now_iso()
    if upload_minio:
        uri = upload_json_payload(bucket_entries, entry_object_key(doc_id, entry), entry)
        print(f"[stitch] pending continuation {uri} next_page={next_page}")


def _orphan_has_text(orphan: Any) -> bool:
    trich = clean_trich_yeu(orphan)
    return bool(trich["han_nom"] or trich["quoc_ngu"])


def _concat_trich(base: dict[str, str], addition: dict[str, str]) -> dict[str, str]:
    # Append continuation columns with newline / Nối cột continuation bằng \\n
    out = {"han_nom": base.get("han_nom", ""), "quoc_ngu": base.get("quoc_ngu", "")}
    for key in ("han_nom", "quoc_ngu"):
        left = str(out.get(key) or "").strip()
        right = str(addition.get(key) or "").strip()
        if left and right:
            out[key] = f"{left}\n{right}"
        else:
            out[key] = left or right
    return out


def _entry_primary_page(entry: dict[str, Any]) -> int | None:
    sources = entry.get("source_pages") or []
    for row in sources:
        if isinstance(row, dict) and row.get("role") in {None, "start", "page_start"}:
            try:
                return int(row["page_no"])
            except (TypeError, ValueError, KeyError):
                pass
    for row in sources:
        if isinstance(row, dict) and row.get("page_no") is not None:
            try:
                return int(row["page_no"])
            except (TypeError, ValueError):
                continue
    return None


def _is_last_on_page(entry: dict[str, Any], page_no: int) -> bool:
    for row in entry.get("source_pages") or []:
        if not isinstance(row, dict):
            continue
        try:
            if int(row.get("page_no")) == page_no and bool(row.get("is_last_on_page")):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _find_nearest_prev_entry(
    entries: list[dict[str, Any]],
    *,
    prev_page: int,
    tap_id: str | None = None,
) -> dict[str, Any] | None:
    """Pick nearest STT on previous page for orphan absorb (same tap only).

    Chọn STT gần nhất trên trang trước để hấp thụ orphan (chỉ cùng tập).
    """
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for entry in entries:
        # Never absorb across volume borders / Không absorb sang tập khác
        entry_tap = entry.get("tap") if isinstance(entry.get("tap"), dict) else {}
        entry_tap_id = entry_tap.get("tap_id") if entry_tap else None
        if tap_id and entry_tap_id and entry_tap_id != tap_id:
            continue
        try:
            stt = int(entry.get("stt"))
        except (TypeError, ValueError):
            continue
        pages = []
        for row in entry.get("source_pages") or []:
            if isinstance(row, dict) and row.get("page_no") is not None:
                try:
                    pages.append(int(row["page_no"]))
                except (TypeError, ValueError):
                    pass
        if prev_page not in pages:
            continue
        # Prefer last-on-page / needs_stitch / incomplete /
        # Ưu tiên cuối trang / cần stitch / incomplete
        score = 0
        if _is_last_on_page(entry, prev_page):
            score += 100
        if _needs_stitch(entry):
            score += 50
        if entry.get("status") in {"incomplete", "needs_stitch", "needs_review", "partial"}:
            score += 20
        scored.append((score, stt, entry))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    best = scored[0]
    # Gate: only absorb into candidates that look incomplete unless sole last-on-page /
    # Chỉ absorb vào candidate cụt, trừ khi là entry cuối trang duy nhất
    if best[0] < 50 and not _is_last_on_page(best[2], prev_page):
        return None
    return best[2]


def _merge_chi_muc(base: Any, addition: Any) -> dict[str, Any]:
    # Fill empty chi_muc fields from continuation / Điền field chi_muc trống từ continuation
    out: dict[str, Any] = dict(base or {})
    add = addition or {}
    for key in ("ngay_thang", "to_tap", "the_loai", "xuat_xu", "de_tai"):
        cur = out.get(key)
        nxt = add.get(key)
        empty = cur is None or str(cur).strip() in {"", "None", "null"}
        if empty and nxt is not None and str(nxt).strip() not in {"", "None", "null"}:
            out[key] = nxt
    return out


def _apply_merged_entry(
    entry: dict[str, Any],
    *,
    trich: dict[str, str],
    append_page: int,
    printed_page: Any,
    role: str,
    notes: str,
    model: str | None,
    confidence: float,
    chi_muc_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources = list(entry.get("source_pages") or [])
    if not any(isinstance(s, dict) and s.get("page_no") == append_page for s in sources):
        sources.append(
            {
                "page_no": int(append_page),
                "printed_page": printed_page,
                "page_type": "muc_luc",
                "role": role,
            }
        )
    chi_muc = _merge_chi_muc(entry.get("chi_muc"), chi_muc_patch)
    flags = detect_entry_flags(chi_muc=chi_muc, trich_yeu=trich, is_last_on_page=False)
    flags = [
        f
        for f in flags
        if f
        not in {
            "needs_stitch",
            "cut_at_trich_yeu_label",
            "cut_mid_entry",
            "pending_continuation",
        }
    ]
    if "page_break_linked" not in flags:
        flags.append("page_break_linked")
    heuristic = estimate_entry_ocr_confidence(chi_muc=chi_muc, trich_yeu=trich, flags=flags)
    stitch_conf = max(confidence, heuristic, 0.5)
    entry_id = str(
        entry.get("entry_id")
        or make_entry_id(
            str(entry.get("doc_id") or "hvb_base"),
            int(entry["stt"]),
            (entry.get("tap") or {}).get("tap_id") if isinstance(entry.get("tap"), dict) else None,
        )
    )
    primary = _entry_primary_page(entry) or append_page
    page_nos = []
    for row in sources:
        if isinstance(row, dict) and row.get("page_no") is not None:
            try:
                p = int(row["page_no"])
            except (TypeError, ValueError):
                continue
            if p not in page_nos:
                page_nos.append(p)
    prev_refine = entry.get("refine_confidence")
    try:
        prev_refine_f = float(prev_refine) if prev_refine is not None else 0.0
    except (TypeError, ValueError):
        prev_refine_f = 0.0
    entry.update(
        {
            "entry_id": entry_id,
            "chi_muc": chi_muc,
            "trich_yeu": trich,
            "content_alignment": [
                {
                    "pair_index": 0,
                    "pair_id": make_entry_pair_id(entry_id, 0),
                    "han_nom": trich["han_nom"],
                    "quoc_ngu": trich["quoc_ngu"],
                    "source_page": primary,
                    "source_pages": page_nos,
                    "source_kind": role,
                }
            ],
            "text_views": {
                "han_nom_full": trich["han_nom"],
                "quoc_ngu_full": trich["quoc_ngu"],
            },
            "source_pages": sources,
            "flags": sorted(set(flags)),
            "refine_confidence": round(max(prev_refine_f, stitch_conf), 4),
            "stitch_confidence": round(min(0.99, stitch_conf), 4),
            "confidence_method": CONFIDENCE_METHOD,
            "stitch_model": model,
            "stitch_at": utc_now_iso(),
            "stitch_notes": notes,
            "status": derive_entry_status(flags, refined=True, stitched=True),
            "pipeline_version": PIPELINE_VERSION,
            "updated_at": utc_now_iso(),
        }
    )
    return entry


def absorb_orphan_heads(
    doc_id: str,
    *,
    pages: str | list[int] | None = None,
    upload_minio: bool = True,
) -> dict[str, int]:
    """Absorb page-start orphan trích yếu into nearest previous STT (upsert).

    Đầu trang là đoạn TY không có index → gắn vào STT gần nhất trang trước (ghi đè entry).
    Hỗ trợ biên batch: chỉ cần OCR trang N + stt_* của trang N-1 đã có trên MinIO.
    """
    cfg = load_config()
    bucket_entries = get_value(cfg, "minio", "bucket_entries", fallback="hvb-entries")
    bucket_ocr = get_value(cfg, "minio", "bucket_ocr", fallback="hvb-ocr")
    page_filter = parse_pages_filter(pages)

    entry_keys = sorted(
        key
        for key in list_objects_with_prefix(bucket=bucket_entries, prefix=f"{doc_id}/", suffix=".json")
        if Path(key).name.startswith("stt_")
    )
    entries = [_load_json(bucket_entries, key) for key in entry_keys]

    # Pages to scan: filter pages + first page of filter (for batch boundary) /
    # Quét trang filter; trang đầu filter cũng absorb orphan từ STT batch trước
    ocr_keys = [
        key
        for key in list_objects_with_prefix(bucket=bucket_ocr, prefix=f"{doc_id}/", suffix=".json")
        if Path(key).name.startswith("page_")
    ]
    totals = {"scanned_pages": 0, "absorbed": 0, "skipped": 0, "failed": 0}
    for key in sorted(ocr_keys):
        try:
            page_no = int(Path(key).stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        if page_filter is not None and page_no not in page_filter:
            continue
        totals["scanned_pages"] += 1
        try:
            ocr = _load_json(bucket_ocr, key)
        except Exception as exc:
            print(f"[absorb] cannot load OCR {key}: {exc}")
            totals["failed"] += 1
            continue

        orphan = clean_trich_yeu(ocr.get("orphan_head"))
        continuation = ocr.get("entry_continuation") if isinstance(ocr.get("entry_continuation"), dict) else None
        cont_trich = clean_trich_yeu((continuation or {}).get("trich_yeu"))
        cont_chi = (continuation or {}).get("chi_muc") or {}
        has_continuation = bool(
            continuation
            and (
                cont_trich["han_nom"]
                or cont_trich["quoc_ngu"]
                or any(cont_chi.get(k) for k in ("to_tap", "the_loai", "xuat_xu", "de_tai", "ngay_thang"))
            )
        )
        if not _orphan_has_text(orphan) and not has_continuation:
            totals["skipped"] += 1
            continue
        prev_page = page_no - 1
        # Do not absorb into previous volume across parent/blank /
        # Không absorb sang tập trước qua trang parent/trắng
        if is_blank_page(doc_id, page_no) or is_tap_parent_page(doc_id, page_no):
            totals["skipped"] += 1
            continue
        if is_blank_page(doc_id, prev_page) or is_tap_parent_page(doc_id, prev_page):
            print(f"[absorb] page={page_no} prev={prev_page} is blank/parent — skip cross-tap absorb")
            totals["skipped"] += 1
            continue

        page_tap = ocr.get("tap") if isinstance(ocr.get("tap"), dict) else tap_payload_for_page(doc_id, page_no)
        tap_id = page_tap.get("tap_id") if page_tap else None
        target = _find_nearest_prev_entry(entries, prev_page=prev_page, tap_id=tap_id)
        if target is None:
            print(f"[absorb] page={page_no} orphan/continuation found but no previous STT on page={prev_page}")
            totals["skipped"] += 1
            continue

        before = clean_trich_yeu(target.get("trich_yeu"))
        addition = cont_trich if has_continuation and (cont_trich["han_nom"] or cont_trich["quoc_ngu"]) else orphan
        if not (addition["han_nom"] or addition["quoc_ngu"]) and not has_continuation:
            totals["skipped"] += 1
            continue
        # Avoid double-absorb if continuation TY already present /
        # Tránh absorb trùng nếu TY continuation đã có
        already = addition["han_nom"] and addition["han_nom"] in before["han_nom"]
        already = already or (addition["quoc_ngu"] and addition["quoc_ngu"] in before["quoc_ngu"])
        if already and not any(
            not (target.get("chi_muc") or {}).get(k) and cont_chi.get(k)
            for k in ("to_tap", "the_loai", "xuat_xu", "de_tai")
        ):
            print(f"[absorb] STT={target.get('stt')} already has continuation from page={page_no}; skip")
            totals["skipped"] += 1
            continue

        trich = _concat_trich(before, addition) if (addition["han_nom"] or addition["quoc_ngu"]) else before
        updated = _apply_merged_entry(
            target,
            trich=trich,
            append_page=page_no,
            printed_page=ocr.get("printed_page"),
            role="entry_continuation" if has_continuation else "orphan_continuation",
            notes=(
                f"absorb entry_continuation from page {page_no}"
                if has_continuation
                else f"absorb orphan_head from page {page_no}"
            ),
            model="rule:entry_continuation" if has_continuation else "rule:orphan_head",
            confidence=0.88 if has_continuation else 0.85,
            chi_muc_patch=cont_chi if has_continuation else None,
        )
        if upload_minio:
            uri = upload_json_payload(
                bucket_entries,
                entry_object_key(doc_id, updated),
                updated,
            )
            print(
                f"[absorb] upsert {uri} pages={[_entry_primary_page(updated), page_no]} "
                f"stt={updated.get('stt')} role={updated.get('stitch_notes')}"
            )
        # Refresh in-memory copy / Cập nhật bản trong RAM
        for idx, row in enumerate(entries):
            if row.get("entry_id") == updated.get("entry_id"):
                entries[idx] = updated
                break
        totals["absorbed"] += 1

    print(f"[absorb] done: {totals}")
    return totals


def stitch_incomplete_entries(
    doc_id: str,
    *,
    pages: str | list[int] | None = None,
    upload_minio: bool = True,
) -> dict[str, int]:
    """Forward stitch + orphan absorb; upserts existing stt_* on MinIO.

    Forward stitch + hấp thụ orphan; ghi đè stt_* đã có (kể cả biên batch).
    """
    # Phase 1: backward absorb orphan heads / Giai đoạn 1: hấp thụ orphan đầu trang
    absorb_totals = absorb_orphan_heads(doc_id, pages=pages, upload_minio=upload_minio)

    cfg = load_config()
    bucket_entries = get_value(cfg, "minio", "bucket_entries", fallback="hvb-entries")
    bucket_ocr = get_value(cfg, "minio", "bucket_ocr", fallback="hvb-ocr")
    page_filter = parse_pages_filter(pages)

    keys = sorted(
        key
        for key in list_objects_with_prefix(bucket=bucket_entries, prefix=f"{doc_id}/", suffix=".json")
        if Path(key).name.startswith("stt_")
    )

    totals = {
        "scanned": 0,
        "stitched": 0,
        "skipped": 0,
        "failed": 0,
        "pending": 0,
        "absorbed": absorb_totals.get("absorbed", 0),
    }
    for key in keys:
        # Reload after absorb upserts / Đọc lại sau khi absorb ghi đè
        entry = _load_json(bucket_entries, key)
        totals["scanned"] += 1
        if not _needs_stitch(entry):
            totals["skipped"] += 1
            continue

        sources = entry.get("source_pages") or []
        if not sources or not isinstance(sources[0], dict):
            totals["skipped"] += 1
            continue
        page_no = sources[0].get("page_no")
        if page_no is None:
            totals["skipped"] += 1
            continue
        page_no = int(page_no)
        # Include entries whose start page is just before filter (batch boundary) /
        # Gồm entry bắt đầu ngay trước filter (biên batch) nếu trang kế nằm trong filter
        next_page = page_no + 1
        if page_filter is not None:
            if page_no not in page_filter and next_page not in page_filter:
                totals["skipped"] += 1
                continue

        label_cut = is_cut_at_trich_yeu_label(
            trich_yeu=entry.get("trich_yeu"),
            is_last_on_page=_is_last_on_page(entry, page_no),
            flags=list(entry.get("flags") or []),
        )

        next_key = v2_ocr_page_key(doc_id, next_page)
        if not object_exists(bucket_ocr, next_key):
            print(f"[stitch] no next OCR page={next_page} (boundary pending)")
            if label_cut:
                _mark_pending_continuation(
                    entry,
                    next_page=next_page,
                    notes=f"label-cut; waiting OCR page {next_page}",
                    upload_minio=upload_minio,
                    bucket_entries=bucket_entries,
                    doc_id=doc_id,
                )
                totals["pending"] += 1
            else:
                totals["failed"] += 1
            continue
        try:
            next_ocr = _load_json(bucket_ocr, next_key)
        except Exception as exc:
            print(f"[stitch] no next OCR page={next_page}: {exc}")
            totals["failed"] += 1
            continue

        next_type = str(next_ocr.get("page_type") or "").lower()
        if next_type in {"blank", "tap_parent"} or is_blank_page(doc_id, next_page) or is_tap_parent_page(doc_id, next_page):
            # Volume/blank boundary — do not stitch across /
            # Biên tập/trắng — không stitch xuyên biên
            print(f"[stitch] next page={next_page} is {next_type or 'blank/parent'}; stop")
            totals["skipped"] += 1
            continue

        # Fast path: entry_continuation / orphan_head → merge without LLM /
        # Đường tắt: entry_continuation / orphan_head → merge không cần LLM
        continuation = (
            next_ocr.get("entry_continuation")
            if isinstance(next_ocr.get("entry_continuation"), dict)
            else None
        )
        cont_trich = clean_trich_yeu((continuation or {}).get("trich_yeu"))
        cont_chi = (continuation or {}).get("chi_muc") or {}
        has_continuation = bool(
            continuation
            and (
                cont_trich["han_nom"]
                or cont_trich["quoc_ngu"]
                or any(cont_chi.get(k) for k in ("to_tap", "the_loai", "xuat_xu", "de_tai", "ngay_thang"))
            )
        )
        orphan = clean_trich_yeu(next_ocr.get("orphan_head"))
        if has_continuation or _orphan_has_text(orphan):
            before = clean_trich_yeu(entry.get("trich_yeu"))
            addition = cont_trich if has_continuation and (cont_trich["han_nom"] or cont_trich["quoc_ngu"]) else orphan
            trich = _concat_trich(before, addition) if (addition["han_nom"] or addition["quoc_ngu"]) else before
            entry = _apply_merged_entry(
                entry,
                trich=trich,
                append_page=next_page,
                printed_page=next_ocr.get("printed_page"),
                role="entry_continuation" if has_continuation else "orphan_continuation",
                notes=(
                    f"forward-stitch via entry_continuation page {next_page}"
                    if has_continuation
                    else f"forward-stitch via orphan_head page {next_page}"
                )
                + (" (label-cut)" if label_cut else "")
                + (" (mid-entry)" if "cut_mid_entry" in set(entry.get("flags") or []) else ""),
                model="rule:entry_continuation" if has_continuation else "rule:orphan_head",
                confidence=0.88 if has_continuation else 0.85,
                chi_muc_patch=cont_chi if has_continuation else None,
            )
            if upload_minio:
                uri = upload_json_payload(
                    bucket_entries, entry_object_key(doc_id, entry), entry
                )
                print(f"[stitch] upsert {uri} via {'entry_continuation' if has_continuation else 'orphan_head'}")
            totals["stitched"] += 1
            continue

        compact_entry = {
            "stt": entry.get("stt"),
            "chi_muc": entry.get("chi_muc"),
            "trich_yeu": entry.get("trich_yeu"),
            "flags": entry.get("flags"),
            "source_page": page_no,
            "cut_at_trich_yeu_label": label_cut,
        }
        compact_next = {
            "page_no": next_ocr.get("page_no"),
            "printed_page": next_ocr.get("printed_page"),
            "orphan_head": next_ocr.get("orphan_head"),
            "entry_continuation": next_ocr.get("entry_continuation"),
            "entries": next_ocr.get("entries") or [],
            "raw_text_preview": str(next_ocr.get("raw_text") or "")[:2500],
        }
        prompt = _STITCH_PROMPT.format(
            entry_json=json.dumps(compact_entry, ensure_ascii=False),
            next_page_json=json.dumps(compact_next, ensure_ascii=False),
        )
        raw, model = call_chat_completion(prompt)
        parsed = extract_json_object(raw) or {}
        notes = str(parsed.get("notes") or "").lower()
        denied = any(
            token in notes
            for token in ("không tìm", "khong tim", "not found", "không phải", "khong phai", "stitched=false")
        )
        if not parsed.get("stitched") or denied:
            print(f"[stitch] STT={entry.get('stt')} not stitched ({parsed.get('notes')})")
            # Label-cut must not be silently skipped / Cắt nhãn không được skip im lặng
            if label_cut or not clean_trich_yeu(entry.get("trich_yeu"))["han_nom"]:
                _mark_pending_continuation(
                    entry,
                    next_page=next_page,
                    notes=f"label-cut pending: {parsed.get('notes') or 'no continuation found'}",
                    upload_minio=upload_minio,
                    bucket_entries=bucket_entries,
                    doc_id=doc_id,
                )
                totals["pending"] += 1
            else:
                totals["skipped"] += 1
            continue

        before = clean_trich_yeu(entry.get("trich_yeu"))
        trich = clean_trich_yeu(parsed.get("trich_yeu"))
        if not trich["han_nom"] and not trich["quoc_ngu"]:
            if label_cut or "cut_mid_entry" in set(entry.get("flags") or []):
                _mark_pending_continuation(
                    entry,
                    next_page=next_page,
                    notes="mid-entry/label-cut; model returned empty trich_yeu",
                    upload_minio=upload_minio,
                    bucket_entries=bucket_entries,
                    doc_id=doc_id,
                )
                totals["pending"] += 1
            else:
                totals["failed"] += 1
            continue
        # Empty base always counts as progress when body appears /
        # Base rỗng mà có body → luôn coi là tiến triển
        progressed = (not before["han_nom"] and not before["quoc_ngu"]) or (
            len(trich["han_nom"]) + len(trich["quoc_ngu"])
            > len(before["han_nom"]) + len(before["quoc_ngu"]) + 5
        )
        chi_patch = parsed.get("chi_muc") if isinstance(parsed.get("chi_muc"), dict) else None
        if chi_patch and not progressed:
            # Metadata-only fill still counts for mid-entry cut /
            # Chỉ bổ sung metadata cũng tính tiến triển với cắt giữa entry
            progressed = True
        if not progressed:
            print(f"[stitch] STT={entry.get('stt')} no text progress; skip")
            if label_cut or "cut_mid_entry" in set(entry.get("flags") or []):
                _mark_pending_continuation(
                    entry,
                    next_page=next_page,
                    notes="mid-entry/label-cut; stitch produced no text progress",
                    upload_minio=upload_minio,
                    bucket_entries=bucket_entries,
                    doc_id=doc_id,
                )
                totals["pending"] += 1
            else:
                totals["skipped"] += 1
            continue

        try:
            stitch_conf = float(parsed.get("stitch_confidence"))
        except (TypeError, ValueError):
            stitch_conf = 0.0
        append_page = int(parsed.get("append_source_page") or next_page)
        entry = _apply_merged_entry(
            entry,
            trich=trich,
            append_page=append_page,
            printed_page=next_ocr.get("printed_page"),
            role="stitch_continuation",
            notes=str(parsed.get("notes") or "")
            + (" [label-cut]" if label_cut else "")
            + (" [mid-entry]" if "cut_mid_entry" in set(entry.get("flags") or []) else ""),
            model=model,
            confidence=stitch_conf,
            chi_muc_patch=chi_patch,
        )
        if upload_minio:
            uri = upload_json_payload(bucket_entries, entry_object_key(doc_id, entry), entry)
            print(f"[stitch] upsert {uri} conf={entry.get('stitch_confidence')} status={entry.get('status')}")
        totals["stitched"] += 1

    print(f"[stitch] done: {totals}")
    return totals
