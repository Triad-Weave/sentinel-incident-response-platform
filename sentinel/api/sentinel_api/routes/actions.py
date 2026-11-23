"""Action approval and rejection routes (FR-IC-04, FR-AG-05)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sentinel_common.db import append_incident_event, notify_local_listeners
from sentinel_common.models import ActionRequest, Incident
from sentinel_common.queue import QUEUE_ACTIONS
from sentinel_common.schemas import ActionDecisionRequest, EventType, IncidentStatus

from sentinel_api.deps import require_responder

router = APIRouter(tags=["actions"])


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@router.post("/actions/{action_id}/approve")
async def approve_action(
    action_id: uuid.UUID,
    body: ActionDecisionRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_responder),
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    queue = request.app.state.queue
    now = datetime.now(tz=UTC)
    publish_action = False
    notifications: list[tuple] = []

    async with session_factory() as sess:
        action = await sess.get(ActionRequest, action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="Action request not found")
        if action.status != "proposed":
            raise HTTPException(
                status_code=409, detail=f"Action request is already '{action.status}'"
            )
        if _as_utc(action.expires_at) < now:
            action.status = "expired"
            await sess.commit()
            raise HTTPException(
                status_code=409, detail="Action request has expired (>15 minutes old)"
            )

        incident = await sess.get(Incident, action.incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")

        action.status = "approved"
        action.decided_by = user["email"]
        action.decision_reason = body.reason.strip()
        action.decided_at = now
        incident.decided_at = now

        if action.action == "none":
            action.status = "succeeded"
            action.executed_at = now
            incident.status = IncidentStatus.MONITORING.value
        else:
            incident.status = IncidentStatus.ACTION_IN_PROGRESS.value
            publish_action = True

        ev1 = await append_incident_event(
            sess,
            incident_id=incident.id,
            event_type=EventType.ACTION_APPROVED.value,
            actor=user["email"],
            payload={
                "action_request_id": str(action.id),
                "action": action.action,
                "service": action.service,
                "reason": action.decision_reason,
            },
        )
        ev2 = await append_incident_event(
            sess,
            incident_id=incident.id,
            event_type=EventType.INCIDENT_STATUS_CHANGED.value,
            actor=user["email"],
            payload={"status": incident.status},
        )
        notifications.extend([(incident.id, ev1.seq), (incident.id, ev2.seq)])
        await sess.commit()

    for inc_id, seq in notifications:
        await notify_local_listeners(inc_id, seq)

    if publish_action:
        await queue.publish(
            QUEUE_ACTIONS,
            {"action_request_id": str(action.id), "action": action.action},
        )

    return {
        "id": str(action.id),
        "status": action.status,
        "incident_status": incident.status,
        "decided_by": action.decided_by,
        "decision_reason": action.decision_reason,
    }


@router.post("/actions/{action_id}/reject")
async def reject_action(
    action_id: uuid.UUID,
    body: ActionDecisionRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_responder),
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    now = datetime.now(tz=UTC)
    notifications: list[tuple] = []

    async with session_factory() as sess:
        action = await sess.get(ActionRequest, action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="Action request not found")
        if action.status != "proposed":
            raise HTTPException(
                status_code=409, detail=f"Action request is already '{action.status}'"
            )
        if _as_utc(action.expires_at) < now:
            action.status = "expired"
            await sess.commit()
            raise HTTPException(
                status_code=409, detail="Action request has expired (>15 minutes old)"
            )

        incident = await sess.get(Incident, action.incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")

        action.status = "rejected"
        action.decided_by = user["email"]
        action.decision_reason = body.reason.strip()
        action.decided_at = now
        incident.decided_at = now
        incident.status = IncidentStatus.REJECTED.value

        ev1 = await append_incident_event(
            sess,
            incident_id=incident.id,
            event_type=EventType.ACTION_REJECTED.value,
            actor=user["email"],
            payload={
                "action_request_id": str(action.id),
                "action": action.action,
                "service": action.service,
                "reason": action.decision_reason,
            },
        )
        ev2 = await append_incident_event(
            sess,
            incident_id=incident.id,
            event_type=EventType.INCIDENT_STATUS_CHANGED.value,
            actor=user["email"],
            payload={"status": IncidentStatus.REJECTED.value},
        )
        notifications.extend([(incident.id, ev1.seq), (incident.id, ev2.seq)])
        await sess.commit()

    for inc_id, seq in notifications:
        await notify_local_listeners(inc_id, seq)

    return {
        "id": str(action.id),
        "status": action.status,
        "incident_status": incident.status,
        "decided_by": action.decided_by,
        "decision_reason": action.decision_reason,
    }
