"""Stored ``valid_post.jsonl`` uploads.

The crawler's export is cumulative, so a normal workflow is: crawl, export,
upload, scan; then later crawl again, export again, upload the (now larger) file
again. The checkpoint in ``app.core.checkpoint`` is what makes that second upload
scan only the new posts rather than the whole corpus.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

log = logging.getLogger(__name__)

FILENAME = "valid_post.jsonl"
# 20k posts of crawler JSON lands well under this; the cap is here so a stray
# upload cannot fill the disk the images need.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
CHUNK = 1024 * 1024


class UploadTooLarge(ValueError):
    pass


@dataclass
class Upload:
    upload_id: str
    path: Path
    uploaded_at: str
    bytes: int
    original_name: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "uploaded_at": self.uploaded_at,
            "bytes": self.bytes,
            "mb": round(self.bytes / 1048576, 2),
            "original_name": self.original_name,
        }


class UploadStore:
    """One directory per upload under ``<data>/uploads/``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _meta_path(self, upload_id: str) -> Path:
        return self.root / upload_id / "meta.json"

    def save(self, stream: BinaryIO, original_name: str = "") -> Upload:
        """Stream to disk in chunks.

        Never ``read()`` the whole body: a cumulative export is tens of MB and
        this box has 8 GB shared with three OCR workers.
        """
        upload_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        target_dir = self.root / upload_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / FILENAME

        total = 0
        tmp = path.with_suffix(".jsonl.part")
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
            # Rename only once fully written, so a partial upload is never
            # mistaken for a complete one.
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

        upload = Upload(
            upload_id=upload_id,
            path=path,
            uploaded_at=datetime.now(timezone.utc).isoformat(),
            bytes=total,
            original_name=original_name,
        )
        self._meta_path(upload_id).write_text(
            json.dumps(upload.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("stored upload %s (%d bytes)", upload_id, total)
        return upload

    def get(self, upload_id: str) -> Upload | None:
        # upload_id reaches this from a URL path; keep it to our own format.
        if not upload_id.isalnum() and not upload_id.replace("T", "").isdigit():
            return None
        meta = self._meta_path(upload_id)
        path = self.root / upload_id / FILENAME
        if not meta.exists() or not path.exists():
            return None
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return Upload(
            upload_id=upload_id,
            path=path,
            uploaded_at=data.get("uploaded_at", ""),
            bytes=data.get("bytes", 0),
            original_name=data.get("original_name", ""),
        )

    def list(self) -> list[Upload]:
        if not self.root.exists():
            return []
        uploads = []
        for entry in sorted(self.root.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            upload = self.get(entry.name)
            if upload is not None:
                uploads.append(upload)
        return uploads

    def latest(self) -> Upload | None:
        uploads = self.list()
        return uploads[0] if uploads else None

    def delete(self, upload_id: str) -> bool:
        upload = self.get(upload_id)
        if upload is None:
            return False
        shutil.rmtree(upload.path.parent, ignore_errors=True)
        return True
