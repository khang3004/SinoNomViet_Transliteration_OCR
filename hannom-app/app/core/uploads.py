"""Storing the two files the upstream team hands over.

``ground_truth.jsonl`` and ``ground_truth.xlsx`` are uploaded by an admin and
land under ``<data>/uploads/`` under fixed names, so re-uploading a corrected
export simply replaces the previous one and the next ingest picks it up.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

log = logging.getLogger(__name__)

JSONL_NAME = "ground_truth.jsonl"
XLSX_NAME = "ground_truth.xlsx"

# 9k rows of transcription plus a spreadsheet lands far under this; the cap is
# here so a stray upload cannot fill the disk the mirrored images need.
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
CHUNK = 1024 * 1024

JSONL_SUFFIXES = {".jsonl", ".json", ".ndjson"}
XLSX_SUFFIXES = {".xlsx", ".xlsm"}


class UploadTooLarge(ValueError):
    pass


class UnknownUploadKind(ValueError):
    pass


def kind_for(filename: str) -> str:
    """'jsonl' or 'xlsx', decided by extension."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in JSONL_SUFFIXES:
        return "jsonl"
    if suffix in XLSX_SUFFIXES:
        return "xlsx"
    raise UnknownUploadKind(
        f"Expected a .jsonl or .xlsx file, got {filename!r}."
    )


@dataclass
class StoredUpload:
    kind: str
    path: Path
    bytes: int
    uploaded_at: str
    original_name: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.path.name,
            "bytes": self.bytes,
            "mb": round(self.bytes / 1048576, 2),
            "uploaded_at": self.uploaded_at,
            "original_name": self.original_name,
        }


class UploadStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, kind: str) -> Path:
        return self.root / (JSONL_NAME if kind == "jsonl" else XLSX_NAME)

    def save(self, stream: BinaryIO, original_name: str) -> StoredUpload:
        """Stream to disk in chunks, replacing any previous file of that kind.

        Never ``read()`` the whole body: the spreadsheet runs to tens of MB and
        holding it in memory buys nothing.
        """
        kind = kind_for(original_name)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(kind)
        tmp = target.with_suffix(target.suffix + ".part")

        total = 0
        try:
            with open(tmp, "wb") as handle:
                while True:
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise UploadTooLarge(
                            f"upload exceeds {MAX_UPLOAD_BYTES // 1048576} MB"
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            # Rename only once fully written, so a partial upload is never
            # ingested as if it were complete.
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        stored = StoredUpload(
            kind=kind,
            path=target,
            bytes=total,
            uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            original_name=original_name,
        )
        log.info("stored %s upload (%d bytes)", kind, total)
        return stored

    def describe(self) -> list[dict[str, Any]]:
        rows = []
        for kind in ("jsonl", "xlsx"):
            path = self.path_for(kind)
            if path.exists():
                info = path.stat()
                rows.append(
                    StoredUpload(
                        kind=kind,
                        path=path,
                        bytes=info.st_size,
                        uploaded_at=datetime.fromtimestamp(
                            info.st_mtime, tz=timezone.utc
                        ).isoformat(timespec="seconds"),
                    ).to_json()
                )
            else:
                rows.append({"kind": kind, "name": self.path_for(kind).name,
                             "bytes": 0, "mb": 0.0, "uploaded_at": "",
                             "original_name": "", "missing": True})
        return rows
