"""Reviewer accounts, managed by an admin.

Accounts are disabled rather than deleted: every review references its reviewer
by name, and removing the account would orphan that attribution in the export.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import current_user, require_admin
from app.core.users import ROLE_REVIEWER, ROLES, UserError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users")


@router.get("")
async def list_users(request: Request, user: dict = Depends(current_user)):
    """Everyone. Visible to all reviewers — they can see each other's work, so
    the roster is not a secret; nothing here exposes a credential."""
    store = request.app.state.users
    return {
        "users": [u.to_json() for u in store.list()],
        "roles": list(ROLES),
        "me": user["username"],
    }


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    role: str = ROLE_REVIEWER


@router.post("")
async def create_user(
    request: Request, body: CreateUserRequest, user: dict = Depends(require_admin)
):
    try:
        created = request.app.state.users.create(
            body.username,
            body.password,
            role=body.role,
            display_name=body.display_name,
            created_by=user["username"],
        )
    except UserError as exc:
        raise HTTPException(400, str(exc)) from exc
    return created.to_json()


class PasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


@router.post("/{username}/password")
async def set_password(
    request: Request,
    username: str,
    body: PasswordRequest,
    user: dict = Depends(require_admin),
):
    try:
        request.app.state.users.set_password(username, body.password)
    except UserError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "username": username.lower()}


class ActiveRequest(BaseModel):
    active: bool


@router.post("/{username}/active")
async def set_active(
    request: Request,
    username: str,
    body: ActiveRequest,
    user: dict = Depends(require_admin),
):
    try:
        request.app.state.users.set_active(username, body.active)
    except UserError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "username": username.lower(), "active": body.active}
