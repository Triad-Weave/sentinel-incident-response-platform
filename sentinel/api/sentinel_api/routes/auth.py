"""Local sign-in / sign-out / current user routes (FR-IC-01, ADR-011)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sentinel_common.models import User
from sentinel_common.schemas import LoginRequest
from sentinel_common.security import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
    verify_password,
)
from sqlalchemy import select

from sentinel_api.deps import require_viewer

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    cfg = request.app.state.settings

    async with session_factory() as sess:
        user = (
            await sess.execute(select(User).where(User.email == body.email.strip().lower()))
        ).scalar_one_or_none()

    if user is None or not verify_password(user.password_hash, body.password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_session_token(
        cfg.session_secret,
        user_id=str(user.id),
        email=user.email,
        role=user.role,
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
    )
    return {"id": str(user.id), "email": user.email, "role": user.role}


@router.post("/auth/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: dict[str, Any] = Depends(require_viewer)) -> dict[str, Any]:
    return {"id": user["user_id"], "email": user["email"], "role": user["role"]}
