"""Env-driven configuration (12-factor).

Every backend/behaviour switch in this app is controlled by an environment
variable so that NO code change is needed to switch engines, and so the design
maps 1:1 to a Kubernetes ``Secret`` + ``envFrom`` later (no YAML in this task).

Secrets (API keys) are read from the environment too — never hardcoded, never
logged. ``log_key_presence()`` prints only booleans (present / missing).

See AGENTS.md §6 (Backends & config) and §7 (Secure API keys).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("hannom.config")


def _env(name: str, default: str) -> str:
    """Read an env var, trimming whitespace, falling back to ``default``."""
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        logger.warning("Env %s is not an int; using default %d", name, default)
        return default


# Which backend env vars require which API key when set to "api"/"vision".
# Used by validate() to fail fast at worker start (AGENTS.md §7).
_API_KEY_REQUIREMENTS: dict[str, tuple[str, str]] = {
    # backend value -> (env var name holding the key, human label)
    "gemini": ("GOOGLE_API_KEY", "Gemini translation/correction"),
    "vision": ("GOOGLE_VISION_KEY", "Google Vision OCR"),
}


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of all runtime configuration, read from env."""

    # --- backends (AGENTS.md §6) -------------------------------------------
    ocr_backend: str = field(default_factory=lambda: _env("OCR_BACKEND", "paddle"))
    translate_backend: str = field(
        default_factory=lambda: _env("TRANSLATE_BACKEND", "api")
    )
    correct_backend: str = field(default_factory=lambda: _env("CORRECT_BACKEND", "skip"))
    qwen_model: str = field(
        default_factory=lambda: _env("QWEN_MODEL", "Qwen2.5-3B-Instruct")
    )
    # Gemini model id used when TRANSLATE_BACKEND=api (AGENTS.md §6).
    translate_model: str = field(
        default_factory=lambda: _env("TRANSLATE_MODEL", "gemini-2.0-flash")
    )

    # --- work identity / rendering ----------------------------------------
    work_id: str = field(default_factory=lambda: _env("DSG_FFF", "HVB_001"))
    pdf_dpi: int = field(default_factory=lambda: _env_int("PDF_DPI", 300))

    # --- storage paths (state lives in the mounted volume, not the image) --
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "./data"))
    dicts_dir: str = field(default_factory=lambda: _env("DICTS_DIR", "./dicts"))

    # ----------------------------------------------------------------------
    @property
    def uploads_dir(self) -> str:
        return os.path.join(self.data_dir, "uploads")

    @property
    def output_dir(self) -> str:
        return os.path.join(self.data_dir, "output")

    @property
    def work_dir(self) -> str:
        return os.path.join(self.data_dir, "work")

    @property
    def jobs_db(self) -> str:
        # JOBS_DB override lets the SQLite file live OFF the bind mount (e.g. a
        # named Docker volume) — SQLite locking/journals are unreliable on the
        # Docker Desktop Windows bind-mount filesystem.
        return os.environ.get("JOBS_DB", "").strip() or os.path.join(self.data_dir, "jobs.db")

    # ----------------------------------------------------------------------
    def required_api_keys(self) -> list[tuple[str, str]]:
        """Return [(env_var, label)] for keys required by the selected backends."""
        needed: list[tuple[str, str]] = []
        # Translation API backend = Gemini.
        if self.translate_backend == "api":
            needed.append(_API_KEY_REQUIREMENTS["gemini"])
        # Correction API backend = Gemini too.
        if self.correct_backend == "api":
            needed.append(_API_KEY_REQUIREMENTS["gemini"])
        # Vision OCR engine needs its own key.
        if self.ocr_backend == "vision":
            needed.append(_API_KEY_REQUIREMENTS["vision"])
        # De-duplicate while preserving order.
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for env_var, label in needed:
            if env_var not in seen:
                seen.add(env_var)
                out.append((env_var, label))
        return out

    def log_key_presence(self) -> None:
        """Log ONLY whether each relevant key is present — never its value."""
        for env_var, _label in _API_KEY_REQUIREMENTS.values():
            present = bool(os.environ.get(env_var, "").strip())
            logger.info("API key %s present: %s", env_var, present)

    def validate(self) -> None:
        """Fail fast if a selected ``*_BACKEND=api`` lacks its required key.

        Raises:
            RuntimeError: with a clear, value-free message naming the missing key.
        """
        missing: list[str] = []
        for env_var, label in self.required_api_keys():
            if not os.environ.get(env_var, "").strip():
                missing.append(f"{env_var} (needed for {label})")
        if missing:
            raise RuntimeError(
                "Missing required API key(s): "
                + "; ".join(missing)
                + ". Set them in the environment (.env locally, Secret on K8s). "
                "No key values are ever read from code or logged."
            )

    def summary(self) -> dict[str, str | int]:
        """Non-secret summary suitable for logging at startup."""
        return {
            "OCR_BACKEND": self.ocr_backend,
            "TRANSLATE_BACKEND": self.translate_backend,
            "TRANSLATE_MODEL": self.translate_model,
            "CORRECT_BACKEND": self.correct_backend,
            "DSG_FFF": self.work_id,
            "PDF_DPI": self.pdf_dpi,
            "DATA_DIR": self.data_dir,
        }


def load_config() -> Config:
    """Build a :class:`Config` from the current process environment."""
    return Config()
