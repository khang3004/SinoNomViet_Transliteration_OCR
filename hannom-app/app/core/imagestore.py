"""Locating stored images on disk, safely.

``post_id`` originates in crawler data and ends up in a filesystem path, so it is
untrusted input. This lives in ``core`` rather than the route module so the
containment rules are testable without a web framework.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def is_safe_post_id(post_id: str) -> bool:
    """Reject ids containing separators, traversal, or absurd length.

    ``..`` alone is not enough to exclude — a literal dot is legal in an id, so
    the allowlist is on the whole string rather than a denylist of sequences.
    """
    if not SAFE_ID.match(post_id):
        return False
    return ".." not in post_id


def parse_image_filename(filename: str) -> tuple[int, str] | None:
    """``0.jpg`` -> (0, '.jpg'). None when the name is not one we mint."""
    stem, _, ext = filename.rpartition(".")
    if not ext:
        return None
    suffix = f".{ext.lower()}"
    if suffix not in ALLOWED_SUFFIXES or not stem.isdigit():
        return None
    return int(stem), suffix


def shard_for(post_id: str) -> str:
    """Two-char shard: 20k files in one directory is slow to stat."""
    return (post_id[:2] or "00").lower()


def resolve_image_path(
    images_dir: Path, post_id: str, idx: int, suffix: str
) -> Path | None:
    """Locate a stored image, or None.

    Returns None unless the resolved path is genuinely inside ``images_dir``.
    That containment check — not the id regex — is what actually stops a crafted
    post_id from reading arbitrary files.
    """
    if not is_safe_post_id(post_id):
        return None

    try:
        root = images_dir.resolve()
    except OSError:
        return None

    shard = shard_for(post_id)
    # Tolerate a suffix mismatch: the URL may say .jpg while the CDN served .png.
    ordered = [suffix] + [s for s in sorted(ALLOWED_SUFFIXES) if s != suffix]

    for candidate_suffix in ordered:
        candidate = images_dir / shard / f"{post_id}_{idx}{candidate_suffix}"
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            log.warning("rejected path escape for post_id=%r", post_id)
            return None
        if resolved.is_file():
            return resolved
    return None


def content_type_for(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
