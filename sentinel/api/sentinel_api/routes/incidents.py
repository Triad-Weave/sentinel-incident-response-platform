"""Incident list, detail, timeline, SSE streams, retry, resolve, postmortem, and follow-up Q&A routes."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from sentinel_common.db import append_incident_event, notify_local_listeners
from sentinel_common.models import (
    ActionRequest,
    Alert,
    Finding,
    Incident,
    IncidentEvent,
    IncidentQuestion,
    Investigation,
    InvestigationStep,
    Postmortem,
)
from sentinel_common.queue import QUEUE_INVESTIGATIONS
from sentinel_common.schemas import (
    OPEN_INCIDENT_STATUSES,
    EventType,
    FollowupQuestionRequest,
    IncidentStatus,
)
from sentinel_worker.llm_client import GatewayLlmClient, LlmGatewayError, load_prompt
from sentinel_worker.postmortem import render_deterministic_postmortem
from sqlalchemy import select

from sentinel_api.deps import require_responder, require_viewer

router = APIRouter(tags=["incidents"])


@router.get("/incidents/stream")
async def stream_incident_list(
    request: Request,
    _user: dict[str, Any] = Depends(require_viewer),
) -> StreamingResponse:
    broker = request.app.state.broker
    return StreamingResponse(
        broker.stream_incident_list(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/incidents")
async def list_incidents(
    request: Request,
    status: str | None = Query(default=None),
    service: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    _user: dict[str, Any] = Depends(require_viewer),
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as sess:
        stmt = select(Incident).order_by(Incident.opened_at.desc())
        if status == "open":
            stmt = stmt.where(Incident.status.in_(OPEN_INCIDENT_STATUSES))
        elif status and status != "all":
            stmt = stmt.where(Incident.status == status)
        if service and service != "all":
            stmt = stmt.where(Incident.alert_service == service)
        if cursor:
            try:
                cursor_dt = datetime.fromisoformat(cursor)
                stmt = stmt.where(Incident.opened_at < cursor_dt)
            except ValueError:
                pass
        rows = list((await sess.execute(stmt.limit(limit + 1))).scalars().all())
        has_more = len(rows) > limit
        rows = rows[:limit]

        items: list[dict[str, Any]] = []
        for inc in rows:
            finding = (
                await sess.execute(
                    select(Finding)
                    .where(Finding.incident_id == inc.id)
                    .order_by(Finding.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            items.append(
                {
                    "id": str(inc.id),
                    "short_id": inc.short_id,
                    "status": inc.status,
                    "title": inc.title,
                    "alert_service": inc.alert_service,
                    "suspected_service": inc.suspected_service,
                    "started_at": inc.started_at.isoformat(),
                    "opened_at": inc.opened_at.isoformat(),
                    "finding_at": inc.finding_at.isoformat() if inc.finding_at else None,
                    "mitigated_at": inc.mitigated_at.isoformat() if inc.mitigated_at else None,
                    "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
                    "finding_summary": (
                        {
                            "fault_type": finding.fault_type,
                            "confidence": finding.confidence,
                            "action_type": finding.action_type,
                            "suspected_service": finding.suspected_service,
                        }
                        if finding
                        else None
                    ),
                }
            )

    next_cursor = rows[-1].opened_at.isoformat() if has_more and rows else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/incidents/{incident_id}")
async def get_incident(
    incident_id: uuid.UUID,
    request: Request,
    _user: dict[str, Any] = Depends(require_viewer),
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as sess:
        inc = await sess.get(Incident, incident_id)
        if inc is None:
            raise HTTPException(status_code=404, detail="Incident not found")

        alerts = list(
            (await sess.execute(select(Alert).where(Alert.incident_id == incident_id)))
            .scalars()
            .all()
        )
        inv = (
            await sess.execute(
                select(Investigation)
                .where(Investigation.incident_id == incident_id)
                .order_by(Investigation.attempt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        steps: list[InvestigationStep] = []
        if inv is not None:
            steps = list(
                (
                    await sess.execute(
                        select(InvestigationStep)
                        .where(InvestigationStep.investigation_id == inv.id)
                        .order_by(InvestigationStep.seq.asc())
                    )
                )
                .scalars()
                .all()
            )
        finding = (
            await sess.execute(
                select(Finding)
                .where(Finding.incident_id == incident_id)
                .order_by(Finding.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        action = (
            await sess.execute(
                select(ActionRequest)
                .where(ActionRequest.incident_id == incident_id)
                .order_by(ActionRequest.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        questions = list(
            (
                await sess.execute(
                    select(IncidentQuestion)
                    .where(IncidentQuestion.incident_id == incident_id)
                    .order_by(IncidentQuestion.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        pm = await sess.get(Postmortem, incident_id)

    return {
        "incident": {
            "id": str(inc.id),
            "short_id": inc.short_id,
            "status": inc.status,
            "title": inc.title,
            "alert_service": inc.alert_service,
            "suspected_service": inc.suspected_service,
            "parent_incident_id": str(inc.parent_incident_id) if inc.parent_incident_id else None,
            "new_evidence_banner": inc.new_evidence_banner,
            "started_at": inc.started_at.isoformat(),
            "opened_at": inc.opened_at.isoformat(),
            "finding_at": inc.finding_at.isoformat() if inc.finding_at else None,
            "decided_at": inc.decided_at.isoformat() if inc.decided_at else None,
            "mitigated_at": inc.mitigated_at.isoformat() if inc.mitigated_at else None,
            "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
        },
        "alerts": [
            {
                "id": str(a.id),
                "alertname": a.alertname,
                "service": a.service,
                "severity": a.severity,
                "status": a.status,
                "starts_at": a.starts_at.isoformat(),
                "ends_at": a.ends_at.isoformat() if a.ends_at else None,
                "annotations": a.annotations,
            }
            for a in alerts
        ],
        "investigation": (
            {
                "id": str(inv.id),
                "attempt": inv.attempt,
                "status": inv.status,
                "failure_reason": inv.failure_reason,
                "model": inv.model,
                "prompt_version": inv.prompt_version,
                "started_at": inv.started_at.isoformat(),
                "ended_at": inv.ended_at.isoformat() if inv.ended_at else None,
                "tool_calls": inv.tool_calls,
                "input_tokens": inv.input_tokens,
                "cached_tokens": inv.cached_tokens,
                "output_tokens": inv.output_tokens,
                "cost_usd": inv.cost_usd,
                "steps": [
                    {
                        "seq": s.seq,
                        "kind": s.kind,
                        "tool_name": s.tool_name,
                        "summary": s.summary,
                        "evidence_id": s.evidence_id,
                        "reasoning": s.reasoning,
                        "latency_ms": s.latency_ms,
                        "created_at": s.created_at.isoformat(),
                    }
                    for s in steps
                ],
            }
            if inv
            else None
        ),
        "finding": (
            {
                "id": str(finding.id),
                "investigation_id": str(finding.investigation_id),
                "body": finding.body,
                "suspected_service": finding.suspected_service,
                "fault_type": finding.fault_type,
                "confidence": finding.confidence,
                "action_type": finding.action_type,
                "action_service": finding.action_service,
                "limit_reached": finding.limit_reached,
                "guardrail_notes": finding.guardrail_notes,
                "created_at": finding.created_at.isoformat(),
            }
            if finding
            else None
        ),
        "action_request": (
            {
                "id": str(action.id),
                "action": action.action,
                "service": action.service,
                "from_version": action.from_version,
                "status": action.status,
                "decided_by": action.decided_by,
                "decision_reason": action.decision_reason,
                "decided_at": action.decided_at.isoformat() if action.decided_at else None,
                "expires_at": action.expires_at.isoformat(),
                "executed_at": action.executed_at.isoformat() if action.executed_at else None,
                "result": action.result,
            }
            if action
            else None
        ),
        "postmortem_available": pm is not None or inc.status == IncidentStatus.RESOLVED.value,
        "questions": [
            {
                "id": str(q.id),
                "asked_by": q.asked_by,
                "question": q.question,
                "answer": q.answer,
                "model": q.model,
                "created_at": q.created_at.isoformat(),
            }
            for q in questions
        ],
    }


@router.get("/incidents/{incident_id}/events")
async def get_incident_events(
    incident_id: uuid.UUID,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    _user: dict[str, Any] = Depends(require_viewer),
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as sess:
        rows = list(
            (
                await sess.execute(
                    select(IncidentEvent)
                    .where(
                        IncidentEvent.incident_id == incident_id,
                        IncidentEvent.seq > after_seq,
                    )
                    .order_by(IncidentEvent.seq.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return {
        "events": [
            {
                "id": str(ev.id),
                "incident_id": str(ev.incident_id),
                "seq": ev.seq,
                "type": ev.type,
                "actor": ev.actor,
                "payload": ev.payload,
                "created_at": ev.created_at.isoformat(),
            }
            for ev in rows
        ]
    }


@router.get("/incidents/{incident_id}/stream")
async def stream_incident_events(
    incident_id: uuid.UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after_seq: int = Query(default=0, ge=0),
    once: bool = Query(default=False),
    _user: dict[str, Any] = Depends(require_viewer),
) -> StreamingResponse:
    broker = request.app.state.broker
    start_seq = after_seq
    if last_event_id and last_event_id.isdigit():
        start_seq = max(start_seq, int(last_event_id))
    return StreamingResponse(
        broker.stream_incident(incident_id, after_seq=start_seq, once=once),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/investigations/{investigation_id}/steps/{seq}")
async def get_investigation_step(
    investigation_id: uuid.UUID,
    seq: int,
    request: Request,
    _user: dict[str, Any] = Depends(require_viewer),
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    async with session_factory() as sess:
        step = (
            await sess.execute(
                select(InvestigationStep).where(
                    InvestigationStep.investigation_id == investigation_id,
                    InvestigationStep.seq == seq,
                )
            )
        ).scalar_one_or_none()
    if step is None:
        raise HTTPException(status_code=404, detail="Investigation step not found")
    return {
        "id": str(step.id),
        "investigation_id": str(step.investigation_id),
        "seq": step.seq,
        "kind": step.kind,
        "tool_name": step.tool_name,
        "input": step.input,
        "summary": step.summary,
        "output": step.output,
        "evidence_id": step.evidence_id,
        "reasoning": step.reasoning,
        "latency_ms": step.latency_ms,
        "created_at": step.created_at.isoformat(),
    }


@router.post("/incidents/{incident_id}/retry")
async def retry_investigation(
    incident_id: uuid.UUID,
    request: Request,
    user: dict[str, Any] = Depends(require_responder),
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    queue = request.app.state.queue
    async with session_factory() as sess:
        inc = await sess.get(Incident, incident_id)
        if inc is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        if inc.status == IncidentStatus.RESOLVED.value:
            raise HTTPException(status_code=409, detail="Cannot retry a resolved incident")
        inc.status = IncidentStatus.INVESTIGATING.value
        inc.new_evidence_banner = False
        ev = await append_incident_event(
            sess,
            incident_id=incident_id,
            event_type=EventType.INCIDENT_STATUS_CHANGED.value,
            actor=user["email"],
            payload={"status": IncidentStatus.INVESTIGATING.value, "reason": "responder retry"},
        )
        await sess.commit()
    await notify_local_listeners(incident_id, ev.seq)
    await queue.publish(
        QUEUE_INVESTIGATIONS, {"job": "investigate", "incident_id": str(incident_id)}
    )
    return {"incident_id": str(incident_id), "status": IncidentStatus.INVESTIGATING.value}


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(
    incident_id: uuid.UUID,
    request: Request,
    user: dict[str, Any] = Depends(require_responder),
) -> dict[str, Any]:
    session_factory = request.app.state.session_factory
    queue = request.app.state.queue
    now = datetime.now(tz=UTC)
    async with session_factory() as sess:
        inc = await sess.get(Incident, incident_id)
        if inc is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        inc.status = IncidentStatus.RESOLVED.value
        inc.resolved_at = now
        ev = await append_incident_event(
            sess,
            incident_id=incident_id,
            event_type=EventType.INCIDENT_STATUS_CHANGED.value,
            actor=user["email"],
            payload={"status": IncidentStatus.RESOLVED.value, "reason": "closed by responder"},
        )
        await sess.commit()
    await notify_local_listeners(incident_id, ev.seq)
    await queue.publish(
        QUEUE_INVESTIGATIONS, {"job": "postmortem", "incident_id": str(incident_id)}
    )
    return {"incident_id": str(incident_id), "status": IncidentStatus.RESOLVED.value}


@router.get("/incidents/{incident_id}/postmortem.md")
async def export_postmortem(
    incident_id: uuid.UUID,
    request: Request,
    _user: dict[str, Any] = Depends(require_viewer),
) -> Response:
    """Export drafted postmortem as Markdown (Should, FR-IC-05)."""
    session_factory = request.app.state.session_factory
    async with session_factory() as sess:
        inc = await sess.get(Incident, incident_id)
        if inc is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        pm = await sess.get(Postmortem, incident_id)
        if pm is not None:
            md = pm.markdown
        else:
            finding = (
                await sess.execute(
                    select(Finding)
                    .where(Finding.incident_id == incident_id)
                    .order_by(Finding.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            action = (
                await sess.execute(
                    select(ActionRequest)
                    .where(ActionRequest.incident_id == incident_id)
                    .order_by(ActionRequest.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            events = list(
                (
                    await sess.execute(
                        select(IncidentEvent)
                        .where(IncidentEvent.incident_id == incident_id)
                        .order_by(IncidentEvent.seq.asc())
                    )
                )
                .scalars()
                .all()
            )
            md = render_deterministic_postmortem(inc, finding, action, events)

    return PlainTextResponse(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{inc.short_id.lower()}-postmortem.md"'
        },
    )


@router.post("/incidents/{incident_id}/questions", status_code=201)
async def ask_followup_question(
    incident_id: uuid.UUID,
    body: FollowupQuestionRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_responder),
) -> dict[str, Any]:
    """Answer a responder's follow-up question grounded in stored incident context (Could, FR-AG-09)."""
    cfg = request.app.state.settings
    if not cfg.enable_followup_questions:
        raise HTTPException(status_code=404, detail="Follow-up questions disabled")

    session_factory = request.app.state.session_factory
    async with session_factory() as sess:
        inc = await sess.get(Incident, incident_id)
        if inc is None:
            raise HTTPException(status_code=404, detail="Incident not found")
        finding = (
            await sess.execute(
                select(Finding)
                .where(Finding.incident_id == incident_id)
                .order_by(Finding.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        inv = (
            await sess.execute(
                select(Investigation)
                .where(Investigation.incident_id == incident_id)
                .order_by(Investigation.attempt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        steps: list[InvestigationStep] = []
        if inv is not None:
            steps = list(
                (
                    await sess.execute(
                        select(InvestigationStep)
                        .where(InvestigationStep.investigation_id == inv.id)
                        .order_by(InvestigationStep.seq.asc())
                    )
                )
                .scalars()
                .all()
            )

    context = {
        "incident": {"short_id": inc.short_id, "title": inc.title, "status": inc.status},
        "finding": finding.body if finding else None,
        "steps": [
            {
                "evidence_id": s.evidence_id,
                "tool": s.tool_name,
                "summary": s.summary,
                "output": s.output,
            }
            for s in steps
        ],
    }

    llm_client: GatewayLlmClient | None = getattr(request.app.state, "llm_client", None)
    answer = "Unable to reach the LLM gateway; review the stored timeline steps above."
    used_model = cfg.agent_model
    cost_usd = 0.0
    if llm_client is not None:
        try:
            resp = await llm_client.chat_completion(
                model=cfg.agent_model,
                messages=[
                    {"role": "system", "content": load_prompt("followup.md")},
                    {
                        "role": "user",
                        "content": (
                            f"Incident context:\n{json.dumps(context, default=str)}\n\n"
                            f"Responder question: {body.question}"
                        ),
                    },
                ],
                feature="followup",
                temperature=0.0,
            )
            answer = resp.content.strip() or answer
            used_model = resp.model
            cost_usd = resp.cost_usd
        except LlmGatewayError as exc:
            raise HTTPException(status_code=503, detail=f"LLM gateway unavailable: {exc}") from exc

    async with session_factory() as sess:
        row = IncidentQuestion(
            incident_id=incident_id,
            asked_by=user["email"],
            question=body.question.strip(),
            answer=answer,
            model=used_model,
            cost_usd=cost_usd,
        )
        sess.add(row)
        await sess.commit()
        await sess.refresh(row)

    return {
        "id": str(row.id),
        "asked_by": row.asked_by,
        "question": row.question,
        "answer": row.answer,
        "model": row.model,
        "cost_usd": row.cost_usd,
        "created_at": row.created_at.isoformat(),
    }
