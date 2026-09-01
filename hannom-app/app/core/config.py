"""Env-driven settings (12-factor). No config files, no secrets in code.

Every value comes from the environment so the same image runs locally and on the
VPS with only the env changing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.core.models import Band
from app.core.sampling import DEFAULT_PER_POST_CAP, DEFAULT_TARGETS

log = logging.getLogger(__name__)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number, got {raw!r}") from None


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


def parse_targets(raw: str) -> dict[Band, float]:
    """``exact=0.15,near=0.2,far=0.25,poor=0.25,empty=0.15`` -> band weights.

    Weights need not sum to 1; the sampler normalizes. An unparseable entry is
    dropped with a warning rather than failing startup — a malformed tuning knob
    should not take the service down.
    """
    if not raw:
        return dict(DEFAULT_TARGETS)
    weights: dict[Band, float] = {}
    for chunk in raw.split(","):
        name, _, value = chunk.partition("=")
        name = name.strip().lower()
        if not name:
            continue
        try:
            weights[Band(name)] = float(value)
        except ValueError:
            log.warning("ignoring unrecognised SAMPLE_TARGETS entry: %r", chunk)
    return weights or dict(DEFAULT_TARGETS)


@dataclass(frozen=True)
class DriveConfig:
    """The upstream team's public image folder.

    ``api_key`` is a plain Google API key restricted to the Drive API. It reads
    only what is already public; it is still an env secret because a leaked key
    burns someone's quota.
    """

    folder_id: str = ""
    api_key: str = ""
    timeout_s: float = 60.0
    max_bytes: int = 25 * 1024 * 1024

    @property
    def configured(self) -> bool:
        return bool(self.folder_id and self.api_key)


@dataclass(frozen=True)
class ImageServeConfig:
    """Reviewers' browsers fetch images from us, not from Drive.

    Signed rather than cookie-gated so the same URL works in an <img> tag from
    any of the reviewers' sessions without a preflight.
    """

    public_base_url: str = ""
    signing_secret: str = ""
    ttl_days: int = 7


@dataclass(frozen=True)
class SamplingConfig:
    default_batch: int = 100
    max_batch: int = 1000
    per_post_cap: int = DEFAULT_PER_POST_CAP
    targets: dict[Band, float] = field(default_factory=lambda: dict(DEFAULT_TARGETS))


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    drive: DriveConfig = field(default_factory=DriveConfig)
    images: ImageServeConfig = field(default_factory=ImageServeConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def corpus_dir(self) -> Path:
        return self.data_dir / "corpus"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def users_path(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def drive_index_path(self) -> Path:
        return self.data_dir / "drive_index.json"


def load_settings() -> Settings:
    """Build settings from the environment. Never logs secret values."""
    from app.core.drive import folder_id_from

    return Settings(
        data_dir=Path(_env("DATA_DIR", "/data")),
        drive=DriveConfig(
            # Accept the full folder URL too — that is what gets pasted.
            folder_id=folder_id_from(_env("GOOGLE_DRIVE_FOLDER_ID")),
            api_key=_env("GOOGLE_DRIVE_API_KEY"),
            timeout_s=_env_float("DRIVE_TIMEOUT", 60.0),
            max_bytes=_env_int("DRIVE_MAX_BYTES", 25 * 1024 * 1024),
        ),
        images=ImageServeConfig(
            public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
            signing_secret=_env("IMAGE_SIGNING_SECRET"),
            ttl_days=_env_int("IMAGE_URL_TTL_DAYS", 7),
        ),
        sampling=SamplingConfig(
            default_batch=_env_int("SAMPLE_BATCH", 100),
            max_batch=_env_int("SAMPLE_MAX_BATCH", 1000),
            per_post_cap=_env_int("SAMPLE_PER_POST_CAP", DEFAULT_PER_POST_CAP),
            targets=parse_targets(_env("SAMPLE_TARGETS")),
        ),
    )


def describe_secrets() -> dict[str, bool]:
    """Startup diagnostics: which secrets are PRESENT. Never their values."""
    return {
        "AUTH_SECRET": bool(_env("AUTH_SECRET")),
        "APP_PASSWORD_HASH": bool(_env("APP_PASSWORD_HASH")),
        "IMAGE_SIGNING_SECRET": bool(_env("IMAGE_SIGNING_SECRET")),
        "GOOGLE_DRIVE_API_KEY": bool(_env("GOOGLE_DRIVE_API_KEY")),
    }
