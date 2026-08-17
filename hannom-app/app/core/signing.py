"""HMAC-signed image URLs.

The Gemini batch stage fetches images anonymously from our domain, but the rest
of the app sits behind a session cookie on the public internet. Signing squares
that circle: ``/img/*`` needs no login, yet only URLs we minted are servable.

Lives in ``core`` (not ``api``) because the batch pipeline mints these URLs when
building output records, and ``core`` must not import the web layer.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass


class SigningError(ValueError):
    """Raised at mint time when the secret is missing."""


def _payload(post_id: str, idx: int, expires_at: int) -> bytes:
    return f"{post_id}:{idx}:{expires_at}".encode("utf-8")


def sign(post_id: str, idx: int, expires_at: int, secret: str) -> str:
    if not secret:
        raise SigningError(
            "IMAGE_SIGNING_SECRET is not set — refusing to mint unsigned image URLs."
        )
    return hmac.new(
        secret.encode("utf-8"), _payload(post_id, idx, expires_at), hashlib.sha256
    ).hexdigest()


def verify(post_id: str, idx: int, expires_at: int, signature: str, secret: str) -> bool:
    """Constant-time signature check plus expiry.

    ``compare_digest`` rather than ``==``: a short-circuiting comparison leaks
    how many leading bytes matched, which is enough to forge a signature given
    enough attempts.
    """
    if not secret or not signature:
        return False
    if expires_at <= int(time.time()):
        return False
    try:
        expected = sign(post_id, idx, expires_at, secret)
    except SigningError:
        return False
    return hmac.compare_digest(expected, signature)


@dataclass
class ImageUrlSigner:
    base_url: str
    secret: str
    ttl_days: int = 30

    def expiry_for(self, now: int | None = None) -> int:
        return int(now or time.time()) + self.ttl_days * 86400

    def build(self, post_id: str, idx: int, suffix: str = ".jpg") -> tuple[str, int]:
        """Return (signed_url, expires_at).

        TTL defaults to 30 days because a Gemini batch is not instantaneous — a
        URL that dies mid-batch is a silent failure at the last step of the
        pipeline, long after this stage reported success.
        """
        expires_at = self.expiry_for()
        signature = sign(post_id, idx, expires_at, self.secret)
        base = self.base_url.rstrip("/")
        return (
            f"{base}/img/{post_id}/{idx}{suffix}?exp={expires_at}&sig={signature}",
            expires_at,
        )
