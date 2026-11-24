"""Alert-intake correlation and quiet-period auto-resolve logic (FR-AG-01, ADR-008, BUILD_PROMPT §4).

Kept as plain undecorated functions outside FastAPI routes so both unit tests and `mutmut`
exercise every correlation decision directly:
- Idempotent upsert on `(fingerprint, starts_at)`
- Resolve existing firing alerts (`alert.resolved`)
- Attach firing alert to an open incident (`alert.attached`) + `new_evidence_banner` when a
  different service alerts after a finding
- Open a new incident with 10-minute related-incident link (`parent_incident_id`)
- Auto-resolve Mitigated/Monitoring/Rejected incidents after 2 minutes with all alerts resolved
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sentinel_common.db import append_incident_event, next_incident_short_id, notify_local_listeners
from sentinel_common.models import Alert, Incident
from sentinel_common.queue import QUEUE_INVESTIGATIONS, QueueClient
from sentinel_common.schemas import (
    OPEN_INCIDENT_STATUSES,
    AlertmanagerAlert,
    EventType,
    IncidentStatus,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

RELATED_INCIDENT_WINDOW_MINUTES = 10
AUTO_RESOLVE_QUIET_SECONDS = 120
AUTO_RESOLVE_ELIGIBLE_STATUSES = frozenset(
    {
        IncidentStatus.MITIGATED.value,
        IncidentStatus.MONITORING.value,
        IncidentStatus.REJECTED.value,
    }
)


def as_utc(dt: datetime) -> datetime:
    """Normalize a naive or offset-aware datetime to UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def is_within_related_window(
    resolved_at: datetime | None,
    now: datetime,
    *,
    window_minutes: int = RELATED_INCIDENT_WINDOW_MINUTES,
) -> bool:
    """Return True iff `resolved_at` falls within `window_minutes` before `now`."""
    if resolved_at is None:
        return False
    elapsed = as_utc(now) - as_utc(resolved_at)
    return timedelta(0) <= elapsed <= timedelta(minutes=window_minutes)


def should_raise_new_evidence_banner(
    finding_at: datetime | None,
    alert_service: str,
    incoming_service: str,
) -> bool:
    """Return True when a post-finding alert arrives on a different service."""
    return finding_at is not None and incoming_service != alert_service


def select_incident_title(summary: str | None, alertname: str, service: str) -> str:
    """Choose concise incident title (max 255 chars) from annotation summary or alert metadata."""
    base = summary if summary else f"{alertname} on {service}"
    return base[:255]


def is_auto_resolve_eligible(
    status: str,
    alerts: Sequence[tuple[str, datetime]],
    now: datetime,
    *,
    quiet_period_seconds: int = AUTO_RESOLVE_QUIET_SECONDS,
) -> bool:
    """Return True iff incident status is eligible, alerts exist, none fire, and >= quiet_period_seconds elapsed."""
    if status not in AUTO_RESOLVE_ELIGIBLE_STATUSES:
        return False
    if not alerts:
        return False
    if any(st == "firing" for st, _ in alerts):
        return False
    latest_end = max(as_utc(end_ts) for _, end_ts in alerts)
    return (as_utc(now) - latest_end) >= timedelta(seconds=quiet_period_seconds)


async def ingest_alertmanager_alerts(
    session_factory: async_sessionmaker[AsyncSession],
    queue: QueueClient,
    alerts: Sequence[AlertmanagerAlert],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Correlate a batch of Alertmanager v4 alerts into Sentinel incidents and enqueue investigations."""
    ref_now = as_utc(now) if now is not None else datetime.now(tz=UTC)
    opened_ids: list[str] = []
    attached_ids: list[str] = []
    resolved_count = 0
    jobs_to_publish: list[dict[str, Any]] = []
    notifications: list[tuple[Any, int]] = []

    async with session_factory() as sess:
        for alert_in in alerts:
            starts_at = as_utc(alert_in.startsAt)
            ends_at = as_utc(alert_in.endsAt) if alert_in.endsAt else None
            alertname = alert_in.labels.get("alertname", "UnknownAlert")
            service = alert_in.labels.get("service", "orders")
            severity = alert_in.labels.get("severity", "page")

            existing_alert = (
                await sess.execute(
                    select(Alert).where(
                        Alert.fingerprint == alert_in.fingerprint,
                        Alert.starts_at == starts_at,
                    )
                )
            ).scalar_one_or_none()

            if alert_in.status == "resolved":
                if existing_alert is not None and existing_alert.status != "resolved":
                    existing_alert.status = "resolved"
                    existing_alert.ends_at = ends_at or ref_now
                    ev = await append_incident_event(
                        sess,
                        incident_id=existing_alert.incident_id,
                        event_type=EventType.ALERT_RESOLVED.value,
                        actor="alertmanager",
                        payload={
                            "alertname": existing_alert.alertname,
                            "service": existing_alert.service,
                            "fingerprint": existing_alert.fingerprint,
                        },
                    )
                    notifications.append((existing_alert.incident_id, ev.seq))
                    resolved_count += 1
                continue

            # Idempotency: repeat firing alert with same (fingerprint, starts_at) is a no-op
            if existing_alert is not None:
                continue

            # One open incident per target system (ADR-008)
            open_incident = (
                await sess.execute(
                    select(Incident)
                    .where(Incident.status.in_(OPEN_INCIDENT_STATUSES))
                    .order_by(Incident.opened_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if open_incident is not None:
                sess.add(
                    Alert(
                        incident_id=open_incident.id,
                        fingerprint=alert_in.fingerprint,
                        alertname=alertname,
                        service=service,
                        severity=severity,
                        status="firing",
                        starts_at=starts_at,
                        ends_at=ends_at,
                        labels=alert_in.labels,
                        annotations=alert_in.annotations,
                    )
                )
                if should_raise_new_evidence_banner(
                    open_incident.finding_at, open_incident.alert_service, service
                ):
                    open_incident.new_evidence_banner = True

                ev = await append_incident_event(
                    sess,
                    incident_id=open_incident.id,
                    event_type=EventType.ALERT_ATTACHED.value,
                    actor="alertmanager",
                    payload={
                        "alertname": alertname,
                        "service": service,
                        "severity": severity,
                        "summary": alert_in.annotations.get("summary", ""),
                    },
                )
                notifications.append((open_incident.id, ev.seq))
                attached_ids.append(str(open_incident.id))
                continue

            # Check if an incident resolved within the last 10 minutes -> link via parent_incident_id
            recent_resolved = (
                await sess.execute(
                    select(Incident)
                    .where(Incident.status == IncidentStatus.RESOLVED.value)
                    .order_by(Incident.resolved_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            parent_id = (
                recent_resolved.id
                if recent_resolved is not None
                and is_within_related_window(recent_resolved.resolved_at, ref_now)
                else None
            )

            short_id = await next_incident_short_id(sess)
            title = select_incident_title(alert_in.annotations.get("summary"), alertname, service)
            incident = Incident(
                short_id=short_id,
                status=IncidentStatus.INVESTIGATING.value,
                title=title,
                alert_service=service,
                parent_incident_id=parent_id,
                started_at=starts_at,
            )
            sess.add(incident)
            await sess.flush()

            sess.add(
                Alert(
                    incident_id=incident.id,
                    fingerprint=alert_in.fingerprint,
                    alertname=alertname,
                    service=service,
                    severity=severity,
                    status="firing",
                    starts_at=starts_at,
                    ends_at=ends_at,
                    labels=alert_in.labels,
                    annotations=alert_in.annotations,
                )
            )

            ev = await append_incident_event(
                sess,
                incident_id=incident.id,
                event_type=EventType.INCIDENT_OPENED.value,
                actor="alertmanager",
                payload={
                    "short_id": short_id,
                    "alertname": alertname,
                    "service": service,
                    "severity": severity,
                    "parent_incident_id": str(parent_id) if parent_id else None,
                    "summary": title,
                },
            )
            notifications.append((incident.id, ev.seq))
            opened_ids.append(str(incident.id))
            jobs_to_publish.append({"job": "investigate", "incident_id": str(incident.id)})

        await sess.commit()

    for inc_id, seq in notifications:
        await notify_local_listeners(inc_id, seq)
    for job in jobs_to_publish:
        await queue.publish(QUEUE_INVESTIGATIONS, job)

    return {
        "opened_incidents": opened_ids,
        "attached_incidents": attached_ids,
        "resolved_alerts": resolved_count,
    }


async def auto_resolve_quiet_incidents(
    session_factory: async_sessionmaker[AsyncSession],
    queue: QueueClient | None = None,
    *,
    quiet_period_seconds: int = AUTO_RESOLVE_QUIET_SECONDS,
    now: datetime | None = None,
) -> int:
    """Auto-resolve eligible incidents once all alerts have remained resolved for `quiet_period_seconds`."""
    ref_now = as_utc(now) if now is not None else datetime.now(tz=UTC)
    resolved_count = 0
    resolved_ids: list[Any] = []
    notifications: list[tuple[Any, int]] = []

    async with session_factory() as sess:
        candidates = list(
            (
                await sess.execute(
                    select(Incident).where(Incident.status.in_(AUTO_RESOLVE_ELIGIBLE_STATUSES))
                )
            )
            .scalars()
            .all()
        )
        for inc in candidates:
            alerts = list(
                (await sess.execute(select(Alert).where(Alert.incident_id == inc.id)))
                .scalars()
                .all()
            )
            pairs = [(a.status, as_utc(a.ends_at or a.received_at)) for a in alerts]
            if is_auto_resolve_eligible(
                inc.status, pairs, ref_now, quiet_period_seconds=quiet_period_seconds
            ):
                inc.status = IncidentStatus.RESOLVED.value
                inc.resolved_at = ref_now
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
