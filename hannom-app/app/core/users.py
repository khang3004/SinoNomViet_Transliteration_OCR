"""Reviewer accounts.

The super admin stays where it was — in the environment, as ``APP_USERNAME`` and
``APP_PASSWORD_HASH`` — so the app can always be reached even if this file is
lost or corrupted. Everyone else lives in ``data/users.json``, created by the
admin through the UI.

Passwords are only ever stored as bcrypt hashes. A plaintext password exists in
this process for the length of one request and is never logged, not even at
debug level.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.jsonlog import read_json, write_json

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
MIN_PASSWORD_LEN = 8

ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"
ROLES = (ROLE_ADMIN, ROLE_REVIEWER)


class UserError(ValueError):
    """A problem with the request that the admin can fix and retry."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(plain: str) -> str:
    import bcrypt

    # bcrypt hashes only the first 72 BYTES; truncate explicitly so a long
    # passphrase fails loudly at length checks rather than silently at compare.
    return bcrypt.hashpw(plain.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    import bcrypt

    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("ascii"))
    except Exception:  # noqa: BLE001 - malformed hash is simply not a match
        return False


@dataclass
class User:
    username: str
    role: str = ROLE_REVIEWER
    display_name: str = ""
    active: bool = True
    created_at: str = field(default_factory=_now)
    created_by: str = ""
    last_login_at: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def to_json(self, *, include_hash: str = "") -> dict[str, Any]:
        data = {
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
            "active": self.active,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "last_login_at": self.last_login_at,
        }
        if include_hash:
            data["password_hash"] = include_hash
        return data


class UserStore:
    """File-backed accounts. Single-worker service, so a thread lock suffices."""

    def __init__(self, path: Path, super_admin: str = "") -> None:
        self.path = Path(path)
        self.super_admin = super_admin.strip().lower()
        self._lock = threading.Lock()

    # --- persistence -----------------------------------------------------

    def _read(self) -> dict[str, dict[str, Any]]:
        data = read_json(self.path, default={})
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        write_json(self.path, data)

    # --- queries ---------------------------------------------------------

    def get(self, username: str) -> User | None:
        """The stored account, or a synthesised one for the env super admin.

        The super admin has no row in the file, but every caller still needs a
        User for it — role checks, attribution on created accounts, the reviewer
        dropdown. Synthesising it here keeps that special case in one place.
        """
        name = (username or "").strip().lower()
        if not name:
            return None
        if name == self.super_admin:
            return User(
                username=name,
                role=ROLE_ADMIN,
                display_name="Super admin",
                created_by="environment",
            )
        row = self._read().get(name)
        if row is None:
            return None
        return User(
            username=name,
            role=row.get("role", ROLE_REVIEWER),
            display_name=row.get("display_name", ""),
            active=bool(row.get("active", True)),
            created_at=row.get("created_at", ""),
            created_by=row.get("created_by", ""),
            last_login_at=row.get("last_login_at", ""),
        )

    def list(self) -> list[User]:
        """Every account, super admin first."""
        users: list[User] = []
        if self.super_admin:
            admin = self.get(self.super_admin)
            if admin is not None:
                users.append(admin)
        for name in sorted(self._read()):
            if name == self.super_admin:
                continue
            user = self.get(name)
            if user is not None:
                users.append(user)
        return users

    def reviewer_names(self) -> list[str]:
        return [u.username for u in self.list() if u.active]

    # --- mutations -------------------------------------------------------

    def create(
        self,
        username: str,
        password: str,
        *,
        role: str = ROLE_REVIEWER,
        display_name: str = "",
        created_by: str = "",
    ) -> User:
        name = (username or "").strip().lower()
        if not USERNAME_RE.match(name):
            raise UserError(
                "Username must be 3-32 characters: lowercase letters, digits, "
                "dot, dash or underscore, starting with a letter or digit."
            )
        if role not in ROLES:
            raise UserError(f"Role must be one of: {', '.join(ROLES)}")
        if len(password or "") < MIN_PASSWORD_LEN:
            raise UserError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
        if name == self.super_admin:
            raise UserError("That username is the super admin, set in the environment.")

        with self._lock:
            data = self._read()
            if name in data:
                raise UserError(f"User {name!r} already exists.")
            user = User(
                username=name,
                role=role,
                display_name=display_name.strip(),
                created_by=created_by,
            )
            data[name] = user.to_json(include_hash=hash_password(password))
            self._write(data)

        log.info("created user %r (role=%s) by %r", name, role, created_by or "?")
        return user

    def set_password(self, username: str, password: str) -> None:
        name = (username or "").strip().lower()
        if len(password or "") < MIN_PASSWORD_LEN:
            raise UserError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
        with self._lock:
            data = self._read()
            if name not in data:
                raise UserError(f"No such user: {name!r}")
            data[name]["password_hash"] = hash_password(password)
            self._write(data)
        log.info("password changed for %r", name)

    def set_active(self, username: str, active: bool) -> None:
        """Disable rather than delete: reviews reference the reviewer by name,
        and removing the account would orphan their work in every report."""
        name = (username or "").strip().lower()
        if name == self.super_admin:
            raise UserError("The super admin cannot be disabled.")
        with self._lock:
            data = self._read()
            if name not in data:
                raise UserError(f"No such user: {name!r}")
            data[name]["active"] = bool(active)
            self._write(data)

    def record_login(self, username: str) -> None:
        name = (username or "").strip().lower()
        if name == self.super_admin:
            return
        with self._lock:
            data = self._read()
            if name in data:
                data[name]["last_login_at"] = _now()
                self._write(data)

    # --- authentication --------------------------------------------------

    def authenticate(self, username: str, password: str) -> User | None:
        """Verify a stored account. The super admin is checked by AuthConfig."""
        name = (username or "").strip().lower()
        row = self._read().get(name)
        if row is None:
            # Spend the time anyway: returning early on an unknown username
            # makes valid names measurably faster to probe.
            verify_password(password, "$2b$12$" + "." * 53)
            return None
        if not verify_password(password, row.get("password_hash", "")):
            return None
        if not row.get("active", True):
            return None
        return self.get(name)
