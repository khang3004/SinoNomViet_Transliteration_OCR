"""Reading the upstream team's public Google Drive folder.

Their images live in one shared Drive folder, named ``<post_id>_<idx>.jpg``. To
show one we need its Drive **file id**, so the folder has to be listed once into
a ``filename -> file id`` index. Getting that listing is the whole difficulty
here, and Google offers three doors of which only two open:

* ``files.list`` **with an API key — does not work.** Drive answers 401
  ``"API keys are not supported by this API"``. Unlike most Google APIs it wants
  a principal, not just a project. This is not a misconfiguration to fix.
* ``files.list`` **with a service account — works, and pages.** This is the only
  route that returns a folder of arbitrary size, so it is the recommended one.
* the public ``embeddedfolderview`` page **— works with no credentials at all**,
  but is **hard-capped at 5,500 entries** with no pagination. Fine for a quick
  start or a smaller folder; it silently truncates a larger one, which is why
  ``truncated`` is reported and surfaced in the UI.

**Downloading needs no credentials either way.** A file in a public folder is
readable from ``drive.google.com/uc?export=download&id=…`` anonymously, so the
mirror never authenticates and an index built by either route is enough.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.jsonlog import read_json, write_json
from app.core.postid import slug

log = logging.getLogger(__name__)

LIST_URL = "https://www.googleapis.com/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EMBEDDED_VIEW_URL = "https://drive.google.com/embeddedfolderview"
DOWNLOAD_URL = "https://drive.google.com/uc"
PAGE_SIZE = 1000

# What the public folder page returns before it stops, with no way to ask for
# more. Anything at or above this is assumed truncated.
EMBEDDED_VIEW_LIMIT = 5500

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_ID_IN_URL = re.compile(
    r"/(?:file/d|folders)/([A-Za-z0-9_-]{20,})|[?&]id=([A-Za-z0-9_-]{20,})"
)
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{25,60}$")

# One entry of the public folder page: its link carries the id, and the title
# div that follows carries the filename. Matching them together keeps the two
# lists in step even if an entry is malformed.
_ENTRY = re.compile(
    r"/file/d/([A-Za-z0-9_-]{20,})/view.*?flip-entry-title\">([^<]+)<", re.S
)

CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}
IMAGE_SUFFIXES = set(CONTENT_TYPES)

# Reverse map, with the preferred spelling pinned: naively inverting
# CONTENT_TYPES gives image/jpeg -> ".jpeg" (last key wins), so every mirrored
# JPEG would land as .jpeg while its source filename and URL say .jpg.
SUFFIX_FOR_TYPE = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/bmp": ".bmp", "image/tiff": ".tif",
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


def download_url(file_id: str) -> str:
    """Anonymous download URL for a file in a public folder."""
    return f"{DOWNLOAD_URL}?export=download&id={file_id}"


# --- service account ---------------------------------------------------

@dataclass
class ServiceAccount:
    """A Google service-account key, exchanged for short-lived access tokens.

    Implemented directly rather than pulling in ``google-auth``: it is one
    signed assertion and one token call, and PyJWT is already a dependency.
    """

    client_email: str
    private_key: str
    token_uri: str = TOKEN_URL
    _token: str = field(default="", repr=False)
    _expires_at: float = field(default=0.0, repr=False)

    @classmethod
    def load(cls, source: str) -> "ServiceAccount | None":
        """From a path to the JSON key file, or the JSON itself."""
        raw = (source or "").strip()
        if not raw:
            return None
        try:
            if not raw.startswith("{"):
                raw = Path(raw).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise DriveError(f"Could not read the service-account key: {exc}") from exc

        email = data.get("client_email", "")
        key = data.get("private_key", "")
        if not email or not key:
            raise DriveError(
                "That JSON is not a service-account key — it needs "
                "'client_email' and 'private_key'."
            )
        return cls(
            client_email=email,
            private_key=key,
            token_uri=data.get("token_uri") or TOKEN_URL,
        )

    async def token(self, client: Any) -> str:
        """A cached bearer token, refreshed a minute before it lapses."""
        if self._token and time.time() < self._expires_at - 60:
            return self._token

        try:
            import jwt
        except ModuleNotFoundError as exc:  # pragma: no cover - pinned dependency
            raise DriveError("Missing dependency 'PyJWT'.") from exc

        now = int(time.time())
        try:
            assertion = jwt.encode(
                {
                    "iss": self.client_email,
                    "scope": SCOPE,
                    "aud": self.token_uri,
                    "iat": now,
                    "exp": now + 3600,
                },
                self.private_key,
                algorithm="RS256",
            )
        except Exception as exc:  # noqa: BLE001 - bad key material
            raise DriveError(
                "Could not sign the service-account assertion — check the "
                f"private_key in the JSON. ({type(exc).__name__}: {exc})"
            ) from exc

        response = await client.post(
            self.token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        if response.status_code != 200:
            raise DriveError(
                f"Service account could not get a token ({response.status_code}): "
                f"{response.text[:200]}"
            )
        payload = response.json()
        self._token = payload.get("access_token", "")
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        if not self._token:
            raise DriveError("Google returned no access token.")
        return self._token


# --- the index ---------------------------------------------------------

@dataclass
class RefreshReport:
    method: str = ""
    files: int = 0
    pages: int = 0
    images: int = 0
    skipped_non_image: int = 0
    truncated: bool = False
    error: str = ""
    refreshed_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "method": self.method, "files": self.files, "pages": self.pages,
            "images": self.images, "skipped_non_image": self.skipped_non_image,
            "truncated": self.truncated, "error": self.error,
            "refreshed_at": self.refreshed_at,
        }


@dataclass
class DriveIndex:
    """A cached ``filename -> file id`` map for one Drive folder."""

    path: Path
    folder_id: str = ""
    service_account: str = ""
    timeout_s: float = 120.0
    _entries: dict[str, dict[str, Any]] | None = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        """A folder id is all that is strictly required.

        Without a service account the public listing still works, just capped —
        so this is about whether we can try at all, not whether we can do it well.
        """
        return bool(self.folder_id)

    @property
    def has_service_account(self) -> bool:
        return bool(self.service_account)

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

    def has(self, image_name: str) -> bool:
        return bool(self.file_id_for(image_name))

    def stats(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "folder_id": self.folder_id,
            "indexed": len(self.load()),
            "service_account": self.has_service_account,
            **self.meta(),
        }

    # --- refreshing ---------------------------------------------------

    async def refresh(self, client: Any = None) -> RefreshReport:
        """Rebuild the index, by the best route available."""
        if not self.configured:
            return RefreshReport(
                error="Set GOOGLE_DRIVE_FOLDER_ID to index the shared folder."
            )

        try:
            import httpx
        except ModuleNotFoundError as exc:  # pragma: no cover - pinned dependency
            raise DriveError("Missing dependency 'httpx'.") from exc

        owns_client = client is None
        client = client or httpx.AsyncClient(
            timeout=self.timeout_s, follow_redirects=True
        )
        try:
            if self.has_service_account:
                report, entries = await self._via_service_account(client)
            else:
                report, entries = await self._via_public_page(client)
        except DriveError as exc:
            return RefreshReport(
                method="service_account" if self.has_service_account else "public_page",
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            return RefreshReport(error=f"{type(exc).__name__}: {exc}")
        finally:
            if owns_client:
                await client.aclose()

        if report.error:
            return report

        report.refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json(self.path, {"meta": report.to_json(), "files": entries})
        self._entries = entries
        log.info(
            "drive index: %d images via %s%s",
            report.images, report.method, " (TRUNCATED)" if report.truncated else "",
        )
        return report

    async def _via_service_account(
        self, client: Any
    ) -> tuple[RefreshReport, dict[str, dict[str, Any]]]:
        """``files.list``, paginated — the only route that sees a whole folder."""
        report = RefreshReport(method="service_account")
        account = ServiceAccount.load(self.service_account)
        if account is None:
            raise DriveError("No service-account key configured.")
        token = await account.token(client)

        entries: dict[str, dict[str, Any]] = {}
        page_token = ""
        while True:
            params = {
                "q": f"'{self.folder_id}' in parents and trashed = false",
                "fields": "nextPageToken,files(id,name,mimeType,size)",
                "pageSize": str(PAGE_SIZE),
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token

            response = await client.get(
                LIST_URL, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if response.status_code != 200:
                raise DriveError(_explain(response.status_code, response.text))

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
                    "id": entry.get("id", ""), "mime": mime,
                    "size": int(entry.get("size") or 0),
                }
                report.images += 1

            page_token = payload.get("nextPageToken", "")
            if not page_token:
                break

        return report, entries

    async def _via_public_page(
        self, client: Any
    ) -> tuple[RefreshReport, dict[str, dict[str, Any]]]:
        """The public folder page — no credentials, capped at 5,500 entries."""
        report = RefreshReport(method="public_page")
        response = await client.get(
            EMBEDDED_VIEW_URL,
            params={"id": self.folder_id},
            headers={"User-Agent": BROWSER_UA},
        )
        if response.status_code != 200:
            raise DriveError(
                f"The public folder listing returned HTTP {response.status_code}. "
                "Check that the folder is shared with 'anyone with the link'."
            )

        entries: dict[str, dict[str, Any]] = {}
        for file_id, name in _ENTRY.findall(response.text):
            report.files += 1
            name = name.strip()
            suffix = Path(name).suffix.lower()
            if suffix not in IMAGE_SUFFIXES:
                report.skipped_non_image += 1
                continue
            entries[name] = {"id": file_id, "mime": CONTENT_TYPES[suffix], "size": 0}
            report.images += 1

        report.pages = 1
        # The endpoint stops at its cap without saying so; treat a full page as
        # certainly incomplete rather than reporting a confident wrong total.
        report.truncated = report.files >= EMBEDDED_VIEW_LIMIT
        if not report.files:
            raise DriveError(
                "The public folder listing returned no files. The folder may not "
                "be shared publicly, or the id may be wrong."
            )
        return report, entries


def _explain(status: int, body: str) -> str:
    """Turn a Drive API status into something actionable."""
    snippet = (body or "")[:200]
    if status == 401:
        return (
            "401 from Drive. An API key alone is never accepted by files.list — "
            "it needs a service account. Check the key JSON is valid. "
            f"({snippet})"
        )
    if status == 403:
        return (
            "403 from Drive. Either the Drive API is not enabled for the service "
            "account's project, or the folder is not readable by it — ask the "
            f"owner to share the folder with the service-account email. ({snippet})"
        )
    if status == 404:
        return f"404 — folder not found or not shared. ({snippet})"
    if status == 400:
        return f"400 — malformed folder id or query. ({snippet})"
    return f"HTTP {status} from Drive. ({snippet})"


# --- the local mirror --------------------------------------------------

class DriveImages:
    """Local mirror of the Drive images, filled on demand.

    Downloads are anonymous: a file in a publicly shared folder is readable
    without credentials, so mirroring works even when the index was built by the
    public route and no service account exists.
    """

    def __init__(
        self, index: DriveIndex, images_dir: Path, max_bytes: int = 25 * 1024 * 1024
    ) -> None:
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
        self,
        post_id: str,
        idx: int,
        image_name: str,
        suffix: str = ".jpg",
        client: Any = None,
    ) -> Path:
        """Return a local file for this image, downloading it if needed."""
        existing = self.cached(post_id, idx, suffix)
        if existing is not None:
            return existing

        file_id = self.index.file_id_for(image_name) or file_id_from(image_name)
        if not file_id:
            raise DriveError(
                f"{image_name!r} is not in the Drive index. Re-index the folder; "
                "if the index is truncated, a service account is needed to see "
                "the whole folder."
            )

        try:
            import httpx
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise DriveError("Missing dependency 'httpx'.") from exc

        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        try:
            response = await client.get(
                download_url(file_id), headers={"User-Agent": BROWSER_UA}
            )
            if response.status_code != 200:
                raise DriveError(_explain(response.status_code, response.text))
            data = response.content
            content_type = (
                response.headers.get("content-type") or ""
            ).split(";")[0].strip()
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
        if not content_type.startswith("image/"):
            # Drive serves an HTML interstitial instead of bytes when a file is
            # not actually public. Storing that would mean a broken thumbnail
            # with no explanation.
            raise DriveError(
                f"Drive served {content_type or 'unknown content'} rather than an "
                f"image for {image_name!r} — the file may not be publicly shared."
            )

        resolved = SUFFIX_FOR_TYPE.get(content_type, suffix.lower())
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
