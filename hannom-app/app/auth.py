"""Self-hosted auth: bcrypt passwords + JWT-in-httpOnly-cookie sessions.

The app is the gateway: on login it sets a signed JWT cookie; a middleware gates
every non-public route; handlers that need the identity use the ``current_user`` /
``require_admin`` dependencies. Secrets (``AUTH_SECRET``) come from env, never
logged. No third-party auth service — everything is in Postgres.
"""

from __future__ import annotations

import logging
import time

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request

from pipeline.config import load_config
from pipeline.db import users_repo

logger = logging.getLogger("hannom.auth")

_config = load_config()

COOKIE_NAME = "hannom_session"
_ALG = "HS256"
_TTL_S = 7 * 24 * 3600  # 7-day sessions

# Paths reachable without a session (everything else requires login).
PUBLIC_PATHS = {"/", "/healthz", "/auth/login", "/auth/logout"}
PUBLIC_PREFIXES = ("/static/",)


# --- passwords ---------------------------------------------------------
# bcrypt directly (not passlib, whose 1.7.4 backend detection breaks with
# bcrypt 4.x). bcrypt hashes only the first 72 BYTES, so we truncate explicitly.
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("ascii"))
    except Exception:  # noqa: BLE001 - malformed hash → not verified
        return False


# --- tokens ------------------------------------------------------------
def make_token(user: dict) -> str:
    now = int(time.time())
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user["role"],
        "iat": now,
        "exp": now + _TTL_S,
    }
    return jwt.encode(payload, _config.auth_secret, algorithm=_ALG)


def decode_token(token: str) -> dict | None:
    try:
        p = jwt.decode(token, _config.auth_secret, algorithms=[_ALG])
        return {"id": p["sub"], "username": p["username"], "role": p["role"]}
    except Exception:  # noqa: BLE001 - expired/invalid/tampered
        return None


def user_from_request(request: Request) -> dict | None:
    """Decode the session cookie into a user dict, or None."""
    token = request.cookies.get(COOKIE_NAME)
    return decode_token(token) if token else None


# --- FastAPI dependencies ---------------------------------------------
def current_user(request: Request) -> dict:
    """Require a valid session; return {id, username, role}. 401 otherwise."""
    user = getattr(request.state, "user", None) or user_from_request(request)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


# --- admin seeding -----------------------------------------------------
def seed_admin() -> None:
    """Create the initial admin from ADMIN_USERNAME/ADMIN_PASSWORD if no users
    exist. Idempotent; a no-op (with a warning) if the password isn't set."""
    dsn = _config.database_url
    if not dsn:
        return
    if not _config.auth_secret:
        logger.warning("AUTH_SECRET is empty — login is disabled until it is set.")
    if users_repo.count(dsn) > 0:
        return
    if not _config.admin_password:
        logger.warning(
            "No users yet and ADMIN_PASSWORD is empty — set ADMIN_USERNAME/"
            "ADMIN_PASSWORD to seed the first admin account."
        )
        return
    users_repo.create(
        dsn, _config.admin_username, hash_password(_config.admin_password), role="admin"
    )
    logger.info("Seeded initial admin account %r.", _config.admin_username)
