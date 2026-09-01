"""Locating mirrored images on disk, safely.

Post ids originate in the upstream team's data and end up in a filesystem path,
so they are untrusted input. This lives in ``core`` rather than the route module
so the containment rules are testable without a web framework.

Note the alphabet: paths and URLs carry the *slug* form of a post id (see
``app.core.postid``), never the raw base64 — which may contain ``/`` and would
otherwise create directories.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# The slug alphabet: URL-safe base64 with padding removed.
SAFE_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,512}$")

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}


def is_safe_slug(value: str) -> bool:
    """Reject anything containing separators, traversal, or absurd length."""
    return bool(SAFE_SLUG.match(value or "")) and ".." not in value


def parse_image_filename(filename: str) -> tuple[int, str] | None:
    """``0.jpg`` -> (0, '.jpg'). None when the name is not one we mint."""
    stem, _, ext = filename.rpartition(".")
    if not ext:
        return None
    suffix = f".{ext.lower()}"
    if suffix not in ALLOWED_SUFFIXES or not stem.isdigit():
        return None
    return int(stem), suffix


def shard_for(post_slug: str) -> str:
    """Two-char shard: nine thousand files in one directory is slow to stat."""
    return (post_slug[:2] or "00").lower()


def resolve_image_path(
    images_dir: Path, post_slug: str, idx: int, suffix: str
) -> Path | None:
    """Locate a mirrored image, or None.

    Returns None unless the resolved path is genuinely inside ``images_dir``.
    That containment check — not the alphabet regex — is what actually stops a
    crafted id from reading arbitrary files.
    """
    if not is_safe_slug(post_slug):
        return None

    try:
        root = images_dir.resolve()
    except OSError:
        return None

    shard = shard_for(post_slug)
    # Tolerate a suffix mismatch: the record may say .jpg while Drive served png.
    ordered = [suffix] + [s for s in sorted(ALLOWED_SUFFIXES) if s != suffix]

    for candidate_suffix in ordered:
        candidate = images_dir / shard / f"{post_slug}_{idx}{candidate_suffix}"
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            log.warning("rejected path escape for post slug %r", post_slug)
            return None
        if resolved.is_file():
            return resolved
    return None


def content_type_for(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """(width, height) from image bytes, without a full decode.

    Best-effort: a truncated image still displays and is still reviewable, it
    just has no dimensions to show.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.width, img.height
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return None, None
