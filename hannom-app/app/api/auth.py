"""Single-user session auth: bcrypt password + JWT in an HttpOnly cookie.

Derived from the previous ``app/auth.py``, which had the crypto right but was
wired to Postgres (``users_repo``) and ``pipeline.config``, both since removed.
The password-hashing and token helpers carry over unchanged; the user store is
now a single env-configured account.

This app is internet-facing on its own domain — Tailscale only covers the
VPS -> MinIO hop — so this login IS the security boundary, not a convenience.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

logger = logging.getLogger("hannom.auth")

COOKIE_NAME = "hannom_session"
_ALG = "HS256"
_TTL_S = 7 * 24 * 3600

PUBLIC_PATHS = {"/login", "/healthz", "/favicon.ico"}
# /img/ is public by design: the Gemini stage fetches anonymously. It is guarded
# by an HMAC signature instead of a session (see app/api/routes_images.py).
PUBLIC_PREFIXES = ("/static/", "/img/", "/api/auth/")

# Brute-force protection. In-memory is sound because the service runs a single
# uvicorn worker (job state requires that anyway).
_MAX_FAILURES = 5
_LOCKOUT_S = 300.0


class AuthNotConfigured(RuntimeError):
    """Refuse to serve rather than serve unprotected."""


# --- passwords ---------------------------------------------------------
# bcrypt directly (not passlib, whose 1.7.4 backend detection breaks with
# bcrypt 4.x). bcrypt hashes only the first 72 BYTES, so truncate explicitly.
def hash_password(plain: str) -> str:
    import bcrypt

    return bcrypt.hashpw(plain.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    import bcrypt

    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("ascii"))
    except Exception:  # noqa: BLE001 - malformed hash -> not verified
        return False


# --- tokens ------------------------------------------------------------
def make_token(username: str, secret: str) -> str:
    import jwt

    now = int(time.time())
    return jwt.encode(
        {"sub": username, "iat": now, "exp": now + _TTL_S}, secret, algorithm=_ALG
    )


def decode_token(token: str, secret: str) -> dict | None:
    import jwt

    try:
        payload = jwt.decode(token, secret, algorithms=[_ALG])
        return {"username": payload["sub"]}
    except Exception:  # noqa: BLE001 - expired/invalid/tampered
        return None


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


@dataclass
class LoginThrottle:
    """Per-IP failed-login lockout."""

    failures: dict[str, list[float]] = field(default_factory=dict)

    def locked_out(self, ip: str) -> float:
        recent = [t for t in self.failures.get(ip, []) if time.time() - t < _LOCKOUT_S]
        self.failures[ip] = recent
        if len(recent) >= _MAX_FAILURES:
            return _LOCKOUT_S - (time.time() - recent[0])
        return 0.0

    def record_failure(self, ip: str) -> None:
        self.failures.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        self.failures.pop(ip, None)


@dataclass
class AuthConfig:
    secret: str
    username: str
    password_hash: str
    cookie_secure: bool

    @classmethod
    def from_env(cls) -> "AuthConfig":
        return cls(
            secret=os.environ.get("AUTH_SECRET", "").strip(),
            username=os.environ.get("APP_USERNAME", "admin").strip(),
            password_hash=os.environ.get("APP_PASSWORD_HASH", "").strip(),
            cookie_secure=os.environ.get("COOKIE_SECURE", "1").strip().lower()
            in {"1", "true", "yes", "on"},
        )

    def validate(self) -> None:
        """Fail fast rather than serve an unprotected app.

        The previous implementation only logged a warning when AUTH_SECRET was
        missing and kept serving — on a public domain that is a silently
        wide-open admin panel, so it is now a hard startup failure.
        """
        missing = []
        if not self.secret:
            missing.append("AUTH_SECRET")
        if not self.password_hash:
            missing.append("APP_PASSWORD_HASH")
        if missing:
            raise AuthNotConfigured(
                f"Refusing to start: {', '.join(missing)} not set. This app is "
                "internet-facing; see .env.example for how to generate them."
            )
        if len(self.secret) < 32:
            logger.warning(
                "AUTH_SECRET is shorter than 32 chars — generate one with "
                "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`"
            )

    def authenticate(self, username: str, password: str) -> bool:
        # Compare the username in constant time too, so response timing does not
        # reveal whether the account name was right.
        user_ok = hmac.compare_digest(username.encode(), self.username.encode())
        pass_ok = verify_password(password, self.password_hash)
        return user_ok and pass_ok


def user_from_request(request: Request, secret: str) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    return decode_token(token, secret) if token else None


def current_user(request: Request) -> dict:
    """FastAPI dependency: require a valid session."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user
