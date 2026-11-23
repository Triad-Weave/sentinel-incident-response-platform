"""Background watchdog: stuck-run reaper (>45s heartbeat) + auto-resolve 2m after last alert resolves."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel_common.db import append_incident_event, notify_local_listeners
from sentinel_common.models import Alert, Incident, Investigation
from sentinel_common.queue import QUEUE_INVESTIGATIONS, QueueClient
from sentinel_common.schemas import EventType, IncidentStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def sweep_stuck_investigations(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    heartbeat_timeout_seconds: int = 45,
) -> int:
    """Mark running investigations whose heartbeat is older than 45s as failed ('worker restarted')."""
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=heartbeat_timeout_seconds)
    reaped = 0
    notifications: list[tuple] = []

    async with session_factory() as sess:
        running = list(
            (await sess.execute(select(Investigation).where(Investigation.status == "running")))
            .scalars()
            .all()
        )
        for inv in running:
            if _as_utc(inv.heartbeat_at) < cutoff:
                inv.status = "failed"
                inv.failure_reason = "worker restarted"
                inv.ended_at = datetime.now(tz=UTC)
                inc = await sess.get(Incident, inv.incident_id)
                if inc is not None and inc.status == IncidentStatus.INVESTIGATING.value:
                    inc.status = IncidentStatus.INVESTIGATION_FAILED.value
                ev1 = await append_incident_event(
                    sess,
                    incident_id=inv.incident_id,
                    event_type=EventType.INVESTIGATION_FAILED.value,
                    actor="watchdog",
                    payload={"investigation_id": str(inv.id), "reason": "worker restarted"},
                )
                ev2 = await append_incident_event(
                    sess,
                    incident_id=inv.incident_id,
                    event_type=EventType.INCIDENT_STATUS_CHANGED.value,
                    actor="watchdog",
                    payload={"status": IncidentStatus.INVESTIGATION_FAILED.value},
                )
                notifications.extend([(inv.incident_id, ev1.seq), (inv.incident_id, ev2.seq)])
                reaped += 1
        if reaped:
            await sess.commit()

    for inc_id, seq in notifications:
        await notify_local_listeners(inc_id, seq)
    return reaped


async def sweep_auto_resolve_incidents(
    session_factory: async_sessionmaker[AsyncSession],
    queue: QueueClient | None = None,
    *,
    quiet_period_seconds: int = 120,
) -> int:
    """Auto-resolve incidents in Mitigated/Monitoring/Rejected 2 minutes after their last alert resolved."""
    eligible_statuses = (
        IncidentStatus.MITIGATED.value,
        IncidentStatus.MONITORING.value,
        IncidentStatus.REJECTED.value,
    )
    now = datetime.now(tz=UTC)
    resolved_count = 0
    resolved_ids = []
    notifications: list[tuple] = []

    async with session_factory() as sess:
        candidates = list(
            (await sess.execute(select(Incident).where(Incident.status.in_(eligible_statuses))))
            .scalars()
            .all()
        )
        for inc in candidates:
            alerts = list(
                (await sess.execute(select(Alert).where(Alert.incident_id == inc.id)))
                .scalars()
                .all()
            )
            if not alerts:
                continue
            if any(a.status == "firing" for a in alerts):
                continue
            end_times = [_as_utc(a.ends_at or a.received_at) for a in alerts]
            if (now - max(end_times)) >= timedelta(seconds=quiet_period_seconds):
                inc.status = IncidentStatus.RESOLVED.value
                inc.resolved_at = now
                ev = await append_incident_event(
                    sess,
                    incident_id=inc.id,
                    event_type=EventType.INCIDENT_STATUS_CHANGED.value,
                    actor="watchdog",
                    payload={
                        "status": IncidentStatus.RESOLVED.value,
                        "reason": "all alerts resolved for 2 minutes",
                    },
                )
                notifications.append((inc.id, ev.seq))
                resolved_ids.append(inc.id)
                resolved_count += 1
        if resolved_count:
            await sess.commit()

    for inc_id, seq in notifications:
        await notify_local_listeners(inc_id, seq)
    if queue is not None:
        for inc_id in resolved_ids:
            await queue.publish(
                QUEUE_INVESTIGATIONS, {"job": "postmortem", "incident_id": str(inc_id)}
            )
    return resolved_count
