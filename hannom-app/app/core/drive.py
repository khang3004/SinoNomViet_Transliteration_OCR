"""Reading the upstream team's public Google Drive folder.

Their images live in one shared Drive folder, named ``<post_id>_<idx>.jpg``. A
public folder can be *read* anonymously but cannot be *listed* anonymously, so
this module needs a Google API key — a free, restricted browser key is enough,
and it grants nothing beyond reading what is already public.

The folder is listed once into a name -> file-id index on disk. From then on the
index answers every lookup, and images are mirrored to local disk the first time
a reviewer opens them. Nobody waits for nine thousand downloads that a few
hundred-image sample will never touch, and the second reviewer to open an image
gets it from disk.

If the index cannot be built, the app still runs: records simply report their
image as unresolved, and the ingest report says how many.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.jsonlog import read_json, write_json
from app.core.postid import slug

log = logging.getLogger(__name__)

LIST_URL = "https://www.googleapis.com/drive/v3/files"
MEDIA_URL = "https://www.googleapis.com/drive/v3/files/{file_id}"
PAGE_SIZE = 1000

# A Drive file id embedded in a URL, or standing alone.
_ID_IN_URL = re.compile(r"/(?:file/d|folders)/([A-Za-z0-9_-]{20,})|[?&]id=([A-Za-z0-9_-]{20,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{25,60}$")

CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}


class DriveError(RuntimeError):
    """Listing or fetching failed in a way the operator needs to see."""


def folder_id_from(value: str) -> str:
    """Accept a folder id or a full Drive URL and return the id."""
    value = (value or "").strip()
    if not value:
        return ""
    match = _ID_IN_URL.search(value)
    if match:
        return match.group(1) or match.group(2) or ""
    return value if _BARE_ID.match(value) else ""


def file_id_from(value: str) -> str:
    """A Drive file id, if the given value already carries one."""
    value = (value or "").strip()
    if not value:
        return ""
    match = _ID_IN_URL.search(value)
    if match:
        return match.group(1) or match.group(2) or ""
    return value if _BARE_ID.match(value) else ""


@dataclass
class RefreshReport:
    files: int = 0
    pages: int = 0
    images: int = 0
    skipped_non_image: int = 0
    error: str = ""
    refreshed_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "files": self.files, "pages": self.pages, "images": self.images,
            "skipped_non_image": self.skipped_non_image,
            "error": self.error, "refreshed_at": self.refreshed_at,
        }


@dataclass
class DriveIndex:
    """A cached filename -> file-id map for one Drive folder."""

    path: Path
    folder_id: str = ""
    api_key: str = ""
    _entries: dict[str, dict[str, Any]] | None = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        return bool(self.folder_id and self.api_key)

    def load(self) -> dict[str, dict[str, Any]]:
        if self._entries is None:
            data = read_json(self.path, default={}) or {}
            entries = data.get("files") if isinstance(data, dict) else None
            self._entries = entries if isinstance(entries, dict) else {}
        return self._entries

    def meta(self) -> dict[str, Any]:
        data = read_json(self.path, default={}) or {}
        return data.get("meta", {}) if isinstance(data, dict) else {}

    def file_id_for(self, image_name: str) -> str:
        entry = self.load().get((image_name or "").strip())
        return entry.get("id", "") if entry else ""

    def stats(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "folder_id": self.folder_id,
            "indexed": len(self.load()),
            **self.meta(),
        }

    async def refresh(self, client: Any = None) -> RefreshReport:
        """List the folder and rewrite the index.

        Paginated: Drive caps a page at 1000 files, and nine thousand images is
        ten round trips, not one.
        """
        from datetime import datetime, timezone

        report = RefreshReport()
        if not self.configured:
            report.error = (
                "Set GOOGLE_DRIVE_FOLDER_ID and GOOGLE_DRIVE_API_KEY to index the "
                "shared folder."
            )
            return report

        try:
            import httpx
        except ModuleNotFoundError as exc:  # pragma: no cover - pinned dependency
            raise DriveError("Missing dependency 'httpx'.") from exc

        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=30.0)
        entries: dict[str, dict[str, Any]] = {}
        token = ""
        try:
            while True:
                params = {
                    "q": f"'{self.folder_id}' in parents and trashed = false",
                    "key": self.api_key,
                    "fields": "nextPageToken,files(id,name,mimeType,size)",
                    "pageSize": str(PAGE_SIZE),
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }
                if token:
                    params["pageToken"] = token

                response = await client.get(LIST_URL, params=params)
                if response.status_code != 200:
                    report.error = _explain(response.status_code, response.text)
                    log.warning("drive listing failed: %s", report.error)
                    return report

                payload = response.json()
                report.pages += 1
                for entry in payload.get("files", []):
                    report.files += 1
                    name = (entry.get("name") or "").strip()
                    mime = entry.get("mimeType", "")
                    if not name or not mime.startswith("image/"):
                        report.skipped_non_image += 1
                        continue
                    entries[name] = {
                        "id": entry.get("id", ""),
                        "mime": mime,
                        "size": int(entry.get("size") or 0),
                    }
                    report.images += 1

                token = payload.get("nextPageToken", "")
                if not token:
                    break
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            report.error = f"{type(exc).__name__}: {exc}"
            log.warning("drive listing failed: %s", report.error)
            return report
        finally:
            if owns_client:
                await client.aclose()

        report.refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json(self.path, {"meta": report.to_json(), "files": entries})
        self._entries = entries
        log.info("drive index: %d images across %d pages", report.images, report.pages)
        return report


def _explain(status: int, body: str) -> str:
    """Turn a Drive API status into something actionable."""
    snippet = (body or "")[:200]
    if status == 403:
        return (
            "403 from Drive. Either the API key is restricted, or the Drive API "
            f"is not enabled for its project. ({snippet})"
        )
    if status == 404:
        return f"404 — folder not found or not shared publicly. ({snippet})"
    if status == 400:
        return f"400 — malformed folder id or query. ({snippet})"
    return f"HTTP {status} from Drive. ({snippet})"


class DriveImages:
    """Local mirror of the Drive images, filled on demand."""

    def __init__(self, index: DriveIndex, images_dir: Path, max_bytes: int = 25 * 1024 * 1024) -> None:
        self.index = index
        self.images_dir = Path(images_dir)
        self.max_bytes = max_bytes

    def local_path(self, post_id: str, idx: int, suffix: str = ".jpg") -> Path:
        # Shard on the id prefix: nine thousand files in one directory is slow
        # to stat on every filesystem we might land on.
        name = slug(post_id)
        shard = (name[:2] or "00").lower()
        return self.images_dir / shard / f"{name}_{idx}{suffix.lower()}"

    def cached(self, post_id: str, idx: int, suffix: str = ".jpg") -> Path | None:
        """An already-mirrored file for this image, whatever extension it took."""
        name = slug(post_id)
        shard = (name[:2] or "00").lower()
        ordered = [suffix.lower()] + [s for s in CONTENT_TYPES if s != suffix.lower()]
        for candidate_suffix in ordered:
            candidate = self.images_dir / shard / f"{name}_{idx}{candidate_suffix}"
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
            except OSError:
                continue
        return None

    async def ensure(
        self, post_id: str, idx: int, image_name: str, suffix: str = ".jpg", client: Any = None
    ) -> Path:
        """Return a local file for this image, downloading it if needed."""
        existing = self.cached(post_id, idx, suffix)
        if existing is not None:
            return existing

        file_id = self.index.file_id_for(image_name) or file_id_from(image_name)
        if not file_id:
            raise DriveError(
                f"{image_name!r} is not in the Drive index. Refresh the index, or "
                "check that the file is in the shared folder."
            )

        try:
            import httpx
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise DriveError("Missing dependency 'httpx'.") from exc

        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        try:
            response = await client.get(
                MEDIA_URL.format(file_id=file_id),
                params={"alt": "media", "key": self.index.api_key},
            )
            if response.status_code != 200:
                raise DriveError(_explain(response.status_code, response.text))
            data = response.content
        except DriveError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DriveError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if not data:
            raise DriveError(f"Drive returned an empty file for {image_name!r}.")
        if len(data) > self.max_bytes:
            raise DriveError(f"{image_name!r} is {len(data)} bytes, over the cap.")

        content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        resolved = {v: k for k, v in CONTENT_TYPES.items()}.get(content_type, suffix.lower())
        path = self.local_path(post_id, idx, resolved)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Temp-then-rename: a half-written file must never be mistaken for a
        # complete mirror by the next request.
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    def mirrored_count(self) -> int:
        if not self.images_dir.exists():
            return 0
        return sum(
            1 for p in self.images_dir.rglob("*")
            if p.is_file() and not p.name.endswith(".part")
        )
