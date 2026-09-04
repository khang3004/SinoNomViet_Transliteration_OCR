"""Process-wide objects the routes share.

One place to construct the stores so routes stay thin and tests can build a
Runtime over a temp directory without a web server.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.audit import AuditStore
from app.core.config import Settings
from app.core.drive import DriveImages, DriveIndex
from app.core.postid import slug
from app.core.signing import ImageUrlSigner, SigningError
from app.core.users import UserStore

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.users = UserStore(settings.users_path, super_admin=_super_admin())
        self.drive_index = DriveIndex(
            path=settings.drive_index_path,
            folder_id=settings.drive.folder_id,
            service_account=settings.drive.service_account,
            timeout_s=settings.drive.timeout_s,
        )
        # The study is drawn only from images the Drive index can resolve, so a
        # truncated index costs coverage rather than handing reviewers blanks.
        self.audit = AuditStore(
            settings.data_dir,
            eligible=lambda record: self.drive_index.has(record.image),
        )
        self.images = DriveImages(
            self.drive_index, settings.images_dir, max_bytes=settings.drive.max_bytes
        )
        self.signer = ImageUrlSigner(
            # Deliberately empty, so every minted URL is a same-origin path.
            # Only reviewers' browsers load these, and baking in a hostname
            # meant one stale PUBLIC_BASE_URL broke every image on the page
            # with no visible reason.
            base_url="",
            secret=settings.images.signing_secret,
            ttl_days=settings.images.ttl_days,
        )

    def image_url(self, post_id: str, idx: int, suffix: str = ".jpg") -> str:
        """A signed URL a reviewer's browser can load directly.

        Returns '' when signing is not configured rather than raising: the rest
        of the page is still useful, and the missing thumbnail is visible.
        """
        try:
            url, _ = self.signer.build(slug(post_id), idx, suffix)
            return url
        except (SigningError, ValueError, TypeError):
            return ""

    async def startup(self) -> None:
        for directory in (
            self.settings.data_dir,
            self.settings.images_dir,
            self.settings.corpus_dir,
            self.settings.uploads_dir,
            self.settings.exports_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    async def shutdown(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "drive": self.drive_index.stats(),
            "mirrored_images": self.images.mirrored_count(),
            "signing_configured": bool(self.settings.images.signing_secret),
            "sampling": {
                "sample_size": self.settings.sampling.sample_size,
                "default_batch": self.settings.sampling.default_batch,
                "max_batch": self.settings.sampling.max_batch,
                "per_post_cap": self.settings.sampling.per_post_cap,
                "targets": {
                    band.value: round(weight, 4)
                    for band, weight in self.settings.sampling.targets.items()
                },
            },
        }


def _super_admin() -> str:
    import os

    return os.environ.get("APP_USERNAME", "admin").strip().lower()
