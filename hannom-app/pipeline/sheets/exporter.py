"""Push the verified corpus into a shared Google Sheet (two tabs).

Split in two so the row-shaping is testable without any network or credentials:

* :func:`build_tables` — pure: corpus rows -> two 2D tables (header + data).
* :func:`sync_corpus` — side-effecting: writes those tables to the spreadsheet via
  a service account (gspread). Imported lazily so the app doesn't need gspread at
  import time and the unit tests can exercise ``build_tables`` on their own.

The service account credentials are supplied by the caller (from env, never
logged). The target spreadsheet must already be shared with the service account.
"""

from __future__ import annotations

# Worksheet (tab) titles. Kept here so both the writer and any test agree on them.
TAB_HAN_VIET = "Hán–Việt"
TAB_DETAIL = "Chi tiết"

_HAN_VIET_HEADER = ["Hán", "Việt"]
_DETAIL_HEADER = [
    "Trang số", "Entry", "Hán", "Việt",
    "Ngày", "Tờ/Tập", "Loại", "Xuất xứ", "Đề tài",
    "Upload", "Người duyệt",
]

# Google Sheets scope only — the sheet is pre-shared, so no Drive scope is needed.
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _cell(v) -> str:
    """Sheets cells are strings; None -> ''. Numbers are stringified as-is."""
    return "" if v is None else str(v)


def build_tables(rows: list[dict]) -> dict[str, list[list[str]]]:
    """Turn ``corpus_repo.export_entries`` rows into the two sheet tables.

    Returns ``{"han_viet": [[...]], "detail": [[...]]}`` — each a list of rows with
    a header first. Pure and deterministic (rows are assumed already ordered).
    """
    han_viet = [list(_HAN_VIET_HEADER)]
    detail = [list(_DETAIL_HEADER)]
    for r in rows:
        han, viet = _cell(r.get("han")), _cell(r.get("meaning"))
        han_viet.append([han, viet])
        detail.append([
            _cell(r.get("page")), _cell(r.get("entry_no")), han, viet,
            _cell(r.get("ngay")), _cell(r.get("to_tap")), _cell(r.get("loai")),
            _cell(r.get("xuat_xu")), _cell(r.get("de_tai")),
            _cell(r.get("job_id")), _cell(r.get("reviewer")),
        ])
    return {"han_viet": han_viet, "detail": detail}


def sync_corpus(credentials_info: dict, sheet_id: str, tables: dict) -> dict:
    """Full-replace the two tabs of ``sheet_id`` with ``tables`` (from build_tables).

    Clears each worksheet and rewrites header + rows. Creates a tab if missing.
    Returns ``{"spreadsheet_url": ..., "sheets": [{"title", "rows"}, ...]}``.
    Raises on auth/API errors (the caller maps them to an HTTP error).
    """
    import gspread  # lazy: only needed when actually syncing
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(credentials_info, scopes=_SCOPES)
    client = gspread.authorize(creds)
    ss = client.open_by_key(sheet_id)

    written = []
    for title, values in ((TAB_HAN_VIET, tables["han_viet"]), (TAB_DETAIL, tables["detail"])):
        n_rows, n_cols = len(values), (len(values[0]) if values else 1)
        try:
            ws = ss.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=title, rows=max(n_rows, 1), cols=max(n_cols, 1))
        ws.clear()
        if values:
            ws.update(values=values, range_name="A1")
        # Data rows (exclude the header) — what the reader actually gets.
        written.append({"title": title, "rows": max(0, n_rows - 1)})
    return {"spreadsheet_url": ss.url, "sheets": written}
