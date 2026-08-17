"""Thin MinIO wrapper.

Ported from ``hvb-processing/dags/jobs/common/io_storage.py``, which already
solved this shape, but env-driven instead of config.ini-driven (that module also
lives outside ``hannom-app/`` and must not be edited — AGENTS.md §0).
"""

from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import urlparse

from app.core.config import MinioConfig

if TYPE_CHECKING:
    from minio import Minio

log = logging.getLogger(__name__)


def parse_endpoint(endpoint: str) -> tuple[str, bool]:
    """Split ``http://host:port`` into (host:port, secure). Accepts a bare
    ``host:port`` too, since MinIO's client wants the host without a scheme."""
    if "://" in endpoint:
        parsed = urlparse(endpoint)
        return (parsed.netloc or parsed.path), parsed.scheme == "https"
    return endpoint, False


class MinioStorage:
    """Object-store operations used by the han_scan stage.

    Append semantics are read-modify-write: object stores have no append, and at
    our volumes (tens of MB of JSONL) rewriting is cheap and keeps every artifact
    a single readable object. Per-batch calls, not per-item.
    """

    def __init__(self, cfg: MinioConfig, client: "Minio" | None = None) -> None:
        self.cfg = cfg
        self._client = client

    @property
    def client(self) -> "Minio":
        if self._client is None:
            try:
                from minio import Minio
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Missing dependency 'minio'. Add it to requirements.txt."
                ) from exc

            if not self.cfg.endpoint:
                raise RuntimeError(
                    "MINIO_ENDPOINT is not set. It must be an address reachable "
                    "from this host (a tailnet NodePort/ingress) — a "
                    "*.svc.cluster.local name will not resolve outside k3s."
                )
            host, secure_from_scheme = parse_endpoint(self.cfg.endpoint)
            self._client = Minio(
                host,
                access_key=self.cfg.access_key,
                secret_key=self.cfg.secret_key,
                secure=self.cfg.secure or secure_from_scheme,
            )
        return self._client

    # --- reads -----------------------------------------------------------

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self.client.stat_object(self.cfg.bucket, key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchBucket"}:
                return False
            raise

    def get_bytes(self, key: str) -> bytes:
        response = None
        try:
            response = self.client.get_object(self.cfg.bucket, key)
            return response.read()
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def get_text(self, key: str) -> str:
        return self.get_bytes(key).decode("utf-8", errors="replace")

    def get_lines(self, key: str) -> list[str]:
        """JSONL as a list of non-empty lines. Missing object -> empty list."""
        from minio.error import S3Error

        try:
            return [ln for ln in self.get_text(key).splitlines() if ln.strip()]
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchBucket"}:
                return []
            raise

    def count_lines(self, key: str) -> int:
        """Used for the progress denominator (valid_post.jsonl)."""
        return len(self.get_lines(key))

    def list_prefixes(self, prefix: str) -> list[str]:
        """Immediate child 'directories' under a prefix — the crawl run ids."""
        self.ensure_bucket()
        normalized = prefix.rstrip("/") + "/"
        out: list[str] = []
        for obj in self.client.list_objects(
            self.cfg.bucket, prefix=normalized, recursive=False
        ):
            name = obj.object_name
            if name.endswith("/"):
                out.append(name.rstrip("/").rsplit("/", 1)[-1])
        return sorted(out)

    def list_objects(self, prefix: str, suffix: str | None = None) -> list[str]:
        self.ensure_bucket()
        keys: list[str] = []
        for obj in self.client.list_objects(
            self.cfg.bucket, prefix=prefix, recursive=True
        ):
            name = obj.object_name
            if name.endswith("/"):
                continue
            if suffix and not name.endswith(suffix):
                continue
            keys.append(name)
        return sorted(keys)

    # --- writes ----------------------------------------------------------

    def ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.cfg.bucket):
            self.client.make_bucket(self.cfg.bucket)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.ensure_bucket()
        self.client.put_object(
            self.cfg.bucket, key, io.BytesIO(data), length=len(data),
            content_type=content_type,
        )

    def put_text(self, key: str, text: str) -> None:
        self.put_bytes(key, text.encode("utf-8"), "text/plain; charset=utf-8")

    def put_json(self, key: str, payload: Any) -> None:
        self.put_bytes(
            key,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def put_jsonl(self, key: str, rows: list[dict[str, Any]]) -> None:
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        if body:
            body += "\n"
        self.put_bytes(key, body.encode("utf-8"), "application/x-ndjson")

    def append_jsonl(self, key: str, rows: list[dict[str, Any]]) -> int:
        """Append rows to a JSONL object, returning the new total line count.

        Read-modify-write. Safe because only this single-worker service writes
        these keys; concurrent writers would need object-lock or per-run keys.
        """
        if not rows:
            return self.count_lines(key)
        existing = self.get_lines(key)
        new_lines = [json.dumps(r, ensure_ascii=False) for r in rows]
        body = "\n".join(existing + new_lines) + "\n"
        self.put_bytes(key, body.encode("utf-8"), "application/x-ndjson")
        return len(existing) + len(new_lines)

    def iter_jsonl(self, key: str) -> Iterator[dict[str, Any]]:
        for line in self.get_lines(key):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                log.warning("skipping malformed JSONL line in %s", key)
