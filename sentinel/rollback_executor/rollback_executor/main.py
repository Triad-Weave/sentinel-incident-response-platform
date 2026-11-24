"""Sentinel Rollback Executor service (FR-AG-05, NFR-04).

- Consumes `sentinel.actions` for `action == "rollback"`.
- Claims rows atomically with conditional UPDATE `approved -> executing` (idempotent on redelivery!).
- Creates a `RollbackRequest` (PB-07) with fixed app map and `expectedCurrentRevision = from_version`.
- On Succeeded -> records result and publishes a `verify` job to `sentinel.investigations`.
- On Failed / timeout -> sets incident back to `Awaiting decision` with error message.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import FastAPI
from platform_telemetry import setup_telemetry
from sentinel_common.config import SentinelSettings
from sentinel_common.db import (
    append_incident_event,
    init_sentinel_schema,
    make_engine,
    make_session_factory,
    notify_local_listeners,
)
from sentinel_common.models import ActionRequest, Incident
from sentinel_common.queue import (
    QUEUE_ACTIONS,
    QUEUE_INVESTIGATIONS,
    QueueClient,
    QueueMessage,
    create_queue_client,
)
from sentinel_common.schemas import EventType, IncidentStatus
from sqlalchemy import CursorResult, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rollback_executor.backends import (
    CrdRollbackBackend,
    GuardedGitPrRollbackBackend,
    HttpShimRollbackBackend,
    RollbackBackend,
)


async def process_rollback_action(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    queue: QueueClient,
    backend: RollbackBackend,
    action_request_id: uuid.UUID,
    timeout_seconds: float = 300.0,
) -> bool:
    """Atomically claim and execute one approved rollback action request."""
    now = datetime.now(tz=UTC)
    async with session_factory() as sess:
        # Conditional claim: ONLY transitions `approved -> executing` once! (Architecture p.19)
        claim_res = cast(
            CursorResult[Any],
            await sess.execute(
                update(ActionRequest)
                .where(
                    ActionRequest.id == action_request_id,
                    ActionRequest.action == "rollback",
                    ActionRequest.status == "approved",
                )
                .values(status="executing", claimed_at=now)
            ),
        )
        if (claim_res.rowcount or 0) == 0:
            await sess.rollback()
            return False

        action = await sess.get(ActionRequest, action_request_id)
        assert action is not None
        incident = await sess.get(Incident, action.incident_id)
        assert incident is not None

        request_name = f"{incident.short_id.lower()}-{action.service}"
        service = action.service or ""
        from_version = action.from_version or ""
        reason = f"{incident.short_id}, approved by {action.decided_by or 'responder'}: {action.decision_reason or ''}"

        ev_exec = await append_incident_event(
            sess,
            incident_id=incident.id,
            event_type=EventType.ACTION_EXECUTING.value,
            actor="rollback-executor",
            payload={
                "action_request_id": str(action.id),
                "request_name": request_name,
                "service": service,
                "from_version": from_version,
            },
        )
        await sess.commit()
    await notify_local_listeners(incident.id, ev_exec.seq)

    outcome = await backend.execute(
        request_name=request_name,
        service=service,
        expected_current_revision=from_version,
        reason=reason,
        timeout_seconds=timeout_seconds,
    )

    done_at = datetime.now(tz=UTC)
    notifications: list[tuple] = []
    async with session_factory() as sess:
        db_action = await sess.get(ActionRequest, action_request_id)
        db_inc = await sess.get(Incident, incident.id)
        assert db_action is not None and db_inc is not None

        db_action.executed_at = done_at
        db_action.result = outcome.to_dict()
        if outcome.succeeded:
            db_action.status = "succeeded"
            ev_done = await append_incident_event(
                sess,
                incident_id=db_inc.id,
                event_type=EventType.ACTION_SUCCEEDED.value,
                actor="rollback-executor",
                payload={"action_request_id": str(db_action.id), **outcome.to_dict()},
            )
            notifications.append((db_inc.id, ev_done.seq))
        else:
            db_action.status = "failed"
            db_inc.status = IncidentStatus.AWAITING_DECISION.value
            ev_fail = await append_incident_event(
                sess,
                incident_id=db_inc.id,
                event_type=EventType.ACTION_FAILED.value,
                actor="rollback-executor",
                payload={"action_request_id": str(db_action.id), **outcome.to_dict()},
            )
            ev_st = await append_incident_event(
                sess,
                incident_id=db_inc.id,
                event_type=EventType.INCIDENT_STATUS_CHANGED.value,
                actor="rollback-executor",
                payload={"status": IncidentStatus.AWAITING_DECISION.value},
            )
            notifications.extend([(db_inc.id, ev_fail.seq), (db_inc.id, ev_st.seq)])
        await sess.commit()

    for inc_id, seq in notifications:
        await notify_local_listeners(inc_id, seq)

    if outcome.succeeded:
        await queue.publish(
            QUEUE_INVESTIGATIONS,
            {"job": "verify", "action_request_id": str(action_request_id)},
        )
    return outcome.succeeded


def select_backend(cfg: SentinelSettings) -> RollbackBackend:
    if cfg.rollback_mode == "crd":
        return CrdRollbackBackend()
    if cfg.rollback_mode == "git_pr":
        return GuardedGitPrRollbackBackend()
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        raise RuntimeError(
            f"Refusing to start rollback-executor with local backend '{cfg.rollback_mode}' "
            "inside Kubernetes (KUBERNETES_SERVICE_HOST is set). Configure SENTINEL_ROLLBACK_MODE=crd."
        )
    return HttpShimRollbackBackend(cfg.pb07_controller_url)


def create_app(settings: SentinelSettings | None = None) -> FastAPI:
    cfg = settings or SentinelSettings()
    engine = make_engine(cfg.database_url)
    session_factory = make_session_factory(engine)
    queue = create_queue_client(cfg.rabbitmq_url, service="rollback-executor")
    backend = select_backend(cfg)

    stop_event = asyncio.Event()
    consumer_task: asyncio.Task[None] | None = None

    async def handle_action(msg: QueueMessage) -> None:
        if msg.payload.get("action") != "rollback":
            # Leave non-rollback actions (e.g., restart) for restart-executor without burning attempts
            if msg.payload.get("action") == "restart":
                await queue.nack(msg, requeue=True, count_attempt=False)
                await asyncio.sleep(0.5)
                return
            await queue.ack(msg)
            return
        act_id = uuid.UUID(str(msg.payload["action_request_id"]))
        await process_rollback_action(
            session_factory=session_factory,
            queue=queue,
            backend=backend,
            action_request_id=act_id,
        )
        await queue.ack(msg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal consumer_task
        await init_sentinel_schema(engine, skip_ddl_on_postgres=True)
        stop_event.clear()
        consumer_task = asyncio.create_task(
            queue.consume(QUEUE_ACTIONS, handle_action, stop_event=stop_event)
        )
        yield
        stop_event.set()
        if consumer_task is not None:
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass
        await queue.close()
        await engine.dispose()

    app = FastAPI(title="Sentinel Rollback Executor", version="1.0.0", lifespan=lifespan)
    setup_telemetry(app, service="rollback-executor", project="sentinel", version="1.0.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        async with session_factory() as sess:
            await sess.execute(text("SELECT 1"))
        return {"status": "ready"}

    return app


app = create_app()
