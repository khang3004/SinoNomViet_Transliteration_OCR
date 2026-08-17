"""Env-driven settings (12-factor). No config files, no secrets in code.

Every value here comes from the environment so the same image runs locally, on
the VPS, and later under K8s with only the env changing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MinioConfig:
    """Where post records are read from and verdicts written back to.

    ``endpoint`` must be reachable from THIS host. A cluster-internal address
    like ``minio.storage.svc.cluster.local:9000`` will not resolve from outside
    the k3s cluster — use the tailnet-reachable NodePort/ingress address.
    """

    endpoint: str = ""
    access_key: str = ""
    secret_key: str = ""
    secure: bool = False
    bucket: str = "final-exam-nlp-raw"
    # Group prefix, e.g. "facebook/322453387859386". Everything below is relative to it.
    group_prefix: str = ""
    output_prefix: str = "han_scan"

    # --- read paths ---
    @property
    def by_run_prefix(self) -> str:
        return f"{self.group_prefix.rstrip('/')}/logs/by_run"

    @property
    def valid_post_key(self) -> str:
        """Cumulative crawler export — used only as the progress denominator."""
        return f"{self.group_prefix.rstrip('/')}/export/valid_post.jsonl"

    # --- write paths (mirrors the crawler's export/ logs/ state/ convention) ---
    @property
    def out_root(self) -> str:
        return f"{self.group_prefix.rstrip('/')}/{self.output_prefix.strip('/')}"

    @property
    def han_valid_key(self) -> str:
        return f"{self.out_root}/export/han_valid.jsonl"

    @property
    def han_invalid_key(self) -> str:
        return f"{self.out_root}/export/han_invalid.jsonl"

    @property
    def failed_key(self) -> str:
        """Cumulative failure log across all runs (for retry sweeps)."""
        return f"{self.out_root}/errors/failed.jsonl"

    @property
    def processed_ids_key(self) -> str:
        return f"{self.out_root}/state/processed_ids.jsonl"

    def run_key(self, scan_run_id: str, name: str) -> str:
        """Per-run artifact: result.json | upserts.jsonl | errors.jsonl."""
        return f"{self.out_root}/logs/by_run/{scan_run_id}/{name}"


@dataclass(frozen=True)
class OcrConfig:
    # PP-OCRv6 is a single unified multilingual model, so there is no per-language
    # model choice the way PP-OCRv4 had. `lang` stays configurable because the
    # fallback path (paddleocr 2.x) still needs it.
    lang: str = "ch"
    min_confidence: float = 0.3
    workers: int = 3
    # Per-image ceiling so one pathological file cannot stall a worker forever.
    timeout_s: float = 120.0
    # Pool drains to fewer workers rather than getting OOM-killed on an 8 GB box.
    memory_limit_mb: int = 6144
    engine_name: str = "paddleocr-3.7.0:PP-OCRv6"


@dataclass(frozen=True)
class DownloadConfig:
    concurrency: int = 16
    timeout_s: float = 30.0
    max_attempts: int = 4
    # Refuse absurd payloads rather than filling the disk.
    max_bytes: int = 25 * 1024 * 1024
    # Adaptive throttle: sustained 429s halve concurrency down to this floor.
    min_concurrency: int = 2
    user_agent: str = "hannom-han-scanner/1.0"


@dataclass(frozen=True)
class ImageServeConfig:
    """Gemini fetches these URLs anonymously, so they are HMAC-signed rather
    than behind the session cookie."""

    public_base_url: str = ""
    signing_secret: str = ""
    ttl_days: int = 30


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    batch_size: int = 500
    scan_interval_s: int = 300
    scheduler_enabled: bool = True
    # Crawler-invalid posts are skipped by default; the exports hold both.
    only_crawler_valid: bool = True

    minio: MinioConfig = field(default_factory=MinioConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    images: ImageServeConfig = field(default_factory=ImageServeConfig)

    @property
    def minio_enabled(self) -> bool:
        """MinIO is entirely optional.

        The default flow is uploading ``valid_post.jsonl`` and downloading
        results, which needs no network path into the k3s cluster. Everything
        MinIO-related stays dormant unless an endpoint is configured.
        """
        return bool(self.minio.endpoint and self.minio.group_prefix)

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def checkpoint_path(self) -> Path:
        return self.state_dir / "processed_ids.jsonl"


def load_settings() -> Settings:
    """Build settings from the environment. Never logs secret values."""
    return Settings(
        data_dir=Path(_env("DATA_DIR", "/data")),
        batch_size=_env_int("BATCH_SIZE", 500),
        scan_interval_s=_env_int("SCAN_INTERVAL", 300),
        scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
        only_crawler_valid=_env_bool("ONLY_CRAWLER_VALID", True),
        minio=MinioConfig(
            endpoint=_env("MINIO_ENDPOINT"),
            access_key=_env("MINIO_ACCESS_KEY"),
            secret_key=_env("MINIO_SECRET_KEY"),
            secure=_env_bool("MINIO_SECURE", False),
            bucket=_env("MINIO_BUCKET", "final-exam-nlp-raw"),
            group_prefix=_env("MINIO_GROUP_PREFIX"),
            output_prefix=_env("MINIO_OUTPUT_PREFIX", "han_scan"),
        ),
        ocr=OcrConfig(
            lang=_env("OCR_LANG", "ch"),
            min_confidence=_env_float("OCR_MIN_CONFIDENCE", 0.3),
            workers=_env_int("OCR_WORKERS", 3),
            timeout_s=_env_float("OCR_TIMEOUT", 120.0),
            memory_limit_mb=_env_int("MEMORY_LIMIT_MB", 6144),
            engine_name=_env("OCR_ENGINE_NAME", "paddleocr-3.7.0:PP-OCRv6"),
        ),
        download=DownloadConfig(
            concurrency=_env_int("DOWNLOAD_CONCURRENCY", 16),
            timeout_s=_env_float("DOWNLOAD_TIMEOUT", 30.0),
            max_attempts=_env_int("DOWNLOAD_MAX_ATTEMPTS", 4),
            max_bytes=_env_int("DOWNLOAD_MAX_BYTES", 25 * 1024 * 1024),
            min_concurrency=_env_int("DOWNLOAD_MIN_CONCURRENCY", 2),
        ),
        images=ImageServeConfig(
            public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
            signing_secret=_env("IMAGE_SIGNING_SECRET"),
            ttl_days=_env_int("IMAGE_URL_TTL_DAYS", 30),
        ),
    )


def describe_secrets() -> dict[str, bool]:
    """Startup diagnostics: which secrets are PRESENT. Never their values."""
    return {
        "AUTH_SECRET": bool(_env("AUTH_SECRET")),
        "APP_PASSWORD_HASH": bool(_env("APP_PASSWORD_HASH")),
        "IMAGE_SIGNING_SECRET": bool(_env("IMAGE_SIGNING_SECRET")),
        "MINIO_ACCESS_KEY": bool(_env("MINIO_ACCESS_KEY")),
        "MINIO_SECRET_KEY": bool(_env("MINIO_SECRET_KEY")),
    }
