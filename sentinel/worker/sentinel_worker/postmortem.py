"""Drafted Markdown postmortem generator (Should, FR-IC-05)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sentinel_common.db import append_incident_event, notify_local_listeners
from sentinel_common.models import ActionRequest, Finding, Incident, IncidentEvent, Postmortem
from sentinel_common.schemas import EventType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel_worker.llm_client import GatewayLlmClient, LlmGatewayError, load_prompt


def render_deterministic_postmortem(
    incident: Incident,
    finding: Finding | None,
    action: ActionRequest | None,
    events: list[IncidentEvent],
) -> str:
    lines = [
        f"# Postmortem: {incident.short_id} — {incident.title}",
        "",
        "## Executive Summary",
        f"- **Status:** {incident.status}",
        f"- **Alerting service:** `{incident.alert_service}`",
        f"- **Suspected root cause service:** `{incident.suspected_service or 'unknown'}`",
        f"- **Started:** {incident.started_at.isoformat()}",
        f"- **Mitigated:** {incident.mitigated_at.isoformat() if incident.mitigated_at else 'N/A'}",
        f"- **Resolved:** {incident.resolved_at.isoformat() if incident.resolved_at else 'N/A'}",
        "",
        "## Root Cause",
    ]
    if finding is not None:
        lines.extend(
            [
                f"- **Fault type:** `{finding.fault_type}` (confidence: `{finding.confidence}`)",
                f"- **Hypothesis:** {finding.body.get('hypothesis', '')}",
                "",
                "### Key Evidence",
            ]
        )
        for ev_item in finding.body.get("evidence", []):
            lines.append(f"- `{ev_item.get('evidence_id')}`: {ev_item.get('claim')}")
    else:
        lines.append("No structured finding recorded.")

    lines.extend(["", "## Resolution & Verification"])
    if action is not None:
        lines.extend(
            [
                f"- **Action:** `{action.action}` on `{action.service or 'none'}` (`{action.status}`)",
                f"- **Decision by:** `{action.decided_by or 'N/A'}` — {action.decision_reason or ''}",
            ]
        )
    else:
        lines.append("No remediation action executed.")

    lines.extend(["", "## Timeline (UTC)"])
    for ev in events:
        lines.append(f"- `{ev.created_at.isoformat()}` — **{ev.type}** ({ev.actor})")

    lines.extend(
        [
            "",
            "## Follow-up Action Items",
            "- [ ] Add automated regression test covering the triggering fault mode.",
            "- [ ] Review alert thresholds and runbook clarity.",
            "",
        ]
    )
    return "\n".join(lines)


async def generate_postmortem(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    llm_client: GatewayLlmClient | None,
    incident_id: uuid.UUID,
    model: str = "gpt-4o-mini",
) -> Postmortem:
    async with session_factory() as sess:
        incident = await sess.get(Incident, incident_id)
        if incident is None:
            raise ValueError(f"Incident {incident_id} not found")

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

    markdown = render_deterministic_postmortem(incident, finding, action, events)
    used_model = "deterministic"

    if llm_client is not None:
        try:
            prompt = load_prompt("postmortem.md")
            context = {
                "short_id": incident.short_id,
                "title": incident.title,
                "status": incident.status,
                "started_at": incident.started_at.isoformat(),
                "mitigated_at": incident.mitigated_at.isoformat()
                if incident.mitigated_at
                else None,
                "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
                "finding": finding.body if finding else None,
                "action": {
                    "action": action.action,
                    "service": action.service,
                    "status": action.status,
                    "decided_by": action.decided_by,
                    "reason": action.decision_reason,
                }
                if action
                else None,
                "timeline": [
                    {"seq": e.seq, "ts": e.created_at.isoformat(), "type": e.type, "actor": e.actor}
                    for e in events
                ],
            }
            resp = await llm_client.chat_completion(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(context, default=str)},
                ],
                feature="postmortem",
                temperature=0.0,
            )
            if resp.content.strip():
                markdown = resp.content.strip() + "\n"
                used_model = resp.model
        except LlmGatewayError:
            pass

    async with session_factory() as sess:
        existing = await sess.get(Postmortem, incident_id)
        if existing is not None:
            existing.markdown = markdown
            existing.model = used_model
            existing.created_at = datetime.now(tz=UTC)
            pm = existing
        else:
            pm = Postmortem(incident_id=incident_id, markdown=markdown, model=used_model)
            sess.add(pm)
        ev = await append_incident_event(
            sess,
            incident_id=incident_id,
            event_type=EventType.POSTMORTEM_DRAFTED.value,
            actor="sentinel-worker",
            payload={"model": used_model},
        )
        await sess.commit()
    await notify_local_listeners(incident_id, ev.seq)
    return pm
