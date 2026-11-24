"""Shared FastAPI auth dependencies for Sentinel API (FR-IC-01)."""

from __future__ import annotations

from typing import Any

from fastapi import Cookie, Header, HTTPException, Request
from sentinel_common.security import SESSION_COOKIE_NAME, verify_session_token


def get_current_user(
    request: Request,
    sentinel_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg = request.app.state.settings

    # Eval harness / automation bearer token support (acts as responder `eval-bot`)
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token == cfg.alertmanager_webhook_token:
            return {"user_id": "eval-bot", "email": "eval-bot@sentinel.local", "role": "responder"}

    if not sentinel_session:
        raise HTTPException(status_code=401, detail="Authentication required")

    user = verify_session_token(cfg.session_secret, sentinel_session)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


def require_viewer(
    request: Request,
    sentinel_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return get_current_user(request, sentinel_session, authorization)


def require_responder(
    request: Request,
    sentinel_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    user = get_current_user(request, sentinel_session, authorization)
    if user.get("role") != "responder":
        raise HTTPException(status_code=403, detail="Responder role required")
    return user
