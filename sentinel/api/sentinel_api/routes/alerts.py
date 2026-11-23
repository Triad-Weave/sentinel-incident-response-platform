"""Alertmanager webhook v4 HTTP route (thin wrapper over `sentinel_api.correlation`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sentinel_common.schemas import AlertmanagerWebhook

from sentinel_api.correlation import ingest_alertmanager_alerts

router = APIRouter(tags=["alerts"])


@router.post("/alerts/alertmanager")
async def alertmanager_webhook(
    payload: AlertmanagerWebhook,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg = request.app.state.settings
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token != cfg.alertmanager_webhook_token:
        raise HTTPException(status_code=403, detail="Invalid alertmanager webhook token")

    return await ingest_alertmanager_alerts(
        request.app.state.session_factory,
        request.app.state.queue,
        payload.alerts,
    )
