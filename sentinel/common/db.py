"""Async SQLAlchemy helpers, append-only event writer, and short_id generator."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sentinel_common.models import Incident, IncidentEvent, SentinelBase
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Optional in-process callback hook used by SSE broker in SQLite/local test mode
EventCallback = Callable[[uuid.UUID, int], Awaitable[None]]
_LOCAL_EVENT_LISTENERS: list[EventCallback] = []


def register_local_event_listener(cb: EventCallback) -> None:
    if cb not in _LOCAL_EVENT_LISTENERS:
        _LOCAL_EVENT_LISTENERS.append(cb)


def unregister_local_event_listener(cb: EventCallback) -> None:
    if cb in _LOCAL_EVENT_LISTENERS:
        _LOCAL_EVENT_LISTENERS.remove(cb)


def make_engine(database_url: str) -> AsyncEngine:
    if database_url.startswith("sqlite"):
        return create_async_engine(database_url)
    return create_async_engine(database_url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_sentinel_schema(engine: AsyncEngine) -> None:
    """Create tables (and Postgres pg_notify trigger if running against PostgreSQL)."""
    async with engine.begin() as conn:
        await conn.run_sync(SentinelBase.metadata.create_all)
        if conn.dialect.name == "postgresql":
            await conn.exec_driver_sql(
                """
                CREATE OR REPLACE FUNCTION notify_incident_event() RETURNS trigger AS $$
                BEGIN
                    PERFORM pg_notify('incident_events', NEW.incident_id::text || ':' || NEW.seq::text);
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;

                DROP TRIGGER IF EXISTS trg_notify_incident_event ON incident_events;
                CREATE TRIGGER trg_notify_incident_event
                    AFTER INSERT ON incident_events
                    FOR EACH ROW EXECUTE FUNCTION notify_incident_event();
                """
            )


async def next_incident_short_id(session: AsyncSession) -> str:
    count = (await session.execute(select(func.count(Incident.id)))).scalar_one()
    return f"INC-{count + 1:04d}"


async def append_incident_event(
    session: AsyncSession,
    *,
    incident_id: uuid.UUID,
    event_type: str,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> IncidentEvent:
    """Append an event with monotonically increasing per-incident `seq`."""
    current_max = (
        await session.execute(
            select(func.coalesce(func.max(IncidentEvent.seq), 0)).where(
                IncidentEvent.incident_id == incident_id
            )
        )
    ).scalar_one()
    event = IncidentEvent(
        incident_id=incident_id,
        seq=int(current_max) + 1,
        type=event_type,
        actor=actor,
        payload=payload or {},
    )
    session.add(event)
    await session.flush()
    return event


async def notify_local_listeners(incident_id: uuid.UUID, seq: int) -> None:
    for cb in list(_LOCAL_EVENT_LISTENERS):
        try:
            await cb(incident_id, seq)
        except Exception:
            pass
