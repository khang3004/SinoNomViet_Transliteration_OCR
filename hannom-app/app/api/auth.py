"""Session auth: bcrypt passwords + JWT in an HttpOnly cookie.

The app is internet-facing on its own domain, so this login IS the security
boundary, not a convenience.

Two account sources, deliberately:

* the **super admin** comes from the environment (``APP_USERNAME`` /
  ``APP_PASSWORD_HASH``) and cannot be created, renamed or disabled through the
  UI — so a mistake in the user file can never lock everyone out;
* **reviewers** live in ``data/users.json`` and are created by an admin.

The role travels inside the signed token. That means a reviewer promoted or
disabled mid-session keeps their old role until the token expires, which is the
trade for not hitting the user file on every request; ``/api/auth/me`` re-reads
the store, and destructive actions re-check it server-side.
"""

from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from app.core.users import ROLE_ADMIN, User, UserStore, hash_password, verify_password

logger = logging.getLogger("hannom.auth")

COOKIE_NAME = "hannom_session"
_ALG = "HS256"
_TTL_S = 7 * 24 * 3600

PUBLIC_PATHS = {"/login", "/healthz", "/favicon.ico"}
# /img/ is signature-guarded rather than cookie-guarded so that <img> tags load
# without a session round-trip (see app/api/routes_images.py).
PUBLIC_PREFIXES = ("/static/", "/img/", "/api/auth/")

_MAX_FAILURES = 5
_LOCKOUT_S = 300.0

__all__ = [
    "COOKIE_NAME", "AuthConfig", "AuthNotConfigured", "LoginThrottle",
    "current_user", "require_admin", "hash_password", "verify_password",
    "is_public_path", "make_token", "decode_token", "user_from_request",
]


class AuthNotConfigured(RuntimeError):
    """Refuse to serve rather than serve unprotected."""


# --- tokens ------------------------------------------------------------
def make_token(username: str, role: str, secret: str) -> str:
    import jwt

    now = int(time.time())
    return jwt.encode(
        {"sub": username, "role": role, "iat": now, "exp": now + _TTL_S},
        secret,
        algorithm=_ALG,
    )


def decode_token(token: str, secret: str) -> dict | None:
    import jwt

    try:
        payload = jwt.decode(token, secret, algorithms=[_ALG])
        return {
            "username": payload["sub"],
            "role": payload.get("role", "reviewer"),
        }
    except Exception:  # noqa: BLE001 - expired/invalid/tampered
        return None


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


@dataclass
class LoginThrottle:
    """Per-IP failed-login lockout. In-memory is sound: one uvicorn worker."""

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
        import os

        return cls(
            secret=os.environ.get("AUTH_SECRET", "").strip(),
            username=os.environ.get("APP_USERNAME", "admin").strip().lower(),
            password_hash=os.environ.get("APP_PASSWORD_HASH", "").strip(),
            cookie_secure=os.environ.get("COOKIE_SECURE", "1").strip().lower()
            in {"1", "true", "yes", "on"},
        )

    def validate(self) -> None:
        """Fail fast rather than serve an unprotected app."""
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

    def authenticate(self, username: str, password: str, users: UserStore) -> User | None:
        """Super admin first, then the reviewer store."""
        name = (username or "").strip().lower()

        # Constant-time on the username too, so response timing does not reveal
        # whether the admin account name was guessed correctly.
        is_admin_name = hmac.compare_digest(name.encode(), self.username.encode())
        if is_admin_name and verify_password(password, self.password_hash):
            return User(username=self.username, role=ROLE_ADMIN, display_name="Super admin")
        if is_admin_name:
            return None
        return users.authenticate(name, password)


def user_from_request(request: Request, secret: str) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    return decode_token(token, secret) if token else None


def current_user(request: Request) -> dict:
    """FastAPI dependency: require a valid session."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def require_admin(request: Request) -> dict:
    """FastAPI dependency: require an admin session.

    Re-reads the user store rather than trusting the token's role alone, so
    revoking an admin takes effect immediately for anything destructive.
    """
    user = current_user(request)
    store: UserStore | None = getattr(request.app.state, "users", None)
    if store is not None:
        stored = store.get(user["username"])
        if stored is None or not stored.is_admin or not stored.active:
            raise HTTPException(status_code=403, detail="admin only")
    elif user.get("role") != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="admin only")
    return user
