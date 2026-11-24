"""Sentinel Restart Executor service (Should, FR-AG-08, Architecture p.20).

Restarts `orders` or `payments` in namespace `shop` by deleting pods ONE AT A TIME and waiting
for each replacement pod to become Ready.
NEVER patches the Deployment (patching drifts from GitOps and RBAC cannot limit patch to one field).
Fenced by RBAC + ValidatingAdmissionPolicy allowing DELETE only on pods labelled
`app.kubernetes.io/name` in `{orders, payments}` inside namespace `shop`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass
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

ALLOWED_RESTART_SERVICES = frozenset({"orders", "payments"})


@dataclass
class RestartOutcome:
    succeeded: bool
    restarted_pods: list[str]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "restarted_pods": self.restarted_pods,
            "message": self.message,
        }


class PodRestartBackend(ABC):
    @abstractmethod
    async def rolling_restart(
        self, service: str, *, timeout_seconds: float = 180.0
    ) -> RestartOutcome: ...


class KubernetesPodRestartBackend(PodRestartBackend):
    """Deletes pods one at a time in namespace `shop` and waits for Deployment availability."""

    NAMESPACE = "shop"

    async def rolling_restart(
        self, service: str, *, timeout_seconds: float = 180.0
    ) -> RestartOutcome:
        if service not in ALLOWED_RESTART_SERVICES:
            return RestartOutcome(
                succeeded=False,
                restarted_pods=[],
                message=f"Service '{service}' is not permitted for rolling restart",
            )

        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()

            core_v1 = client.CoreV1Api()
            apps_v1 = client.AppsV1Api()

            pods = core_v1.list_namespaced_pod(
                namespace=self.NAMESPACE,
                label_selector=f"app.kubernetes.io/name={service}",
            ).items
            pod_names = [p.metadata.name for p in pods if p.metadata and p.metadata.name]
            restarted: list[str] = []
            deadline = time.monotonic() + timeout_seconds

            for pod_name in pod_names:
                core_v1.delete_namespaced_pod(name=pod_name, namespace=self.NAMESPACE)
                restarted.append(pod_name)
                # Wait for replacement pod to reach Ready before deleting the next pod
                while time.monotonic() < deadline:
                    dep = apps_v1.read_namespaced_deployment(name=service, namespace=self.NAMESPACE)
                    desired = dep.spec.replicas or 1 if dep.spec else 1
                    ready = dep.status.ready_replicas or 0 if dep.status else 0
                    current_pods = {
                        p.metadata.name
                        for p in core_v1.list_namespaced_pod(
                            namespace=self.NAMESPACE,
                            label_selector=f"app.kubernetes.io/name={service}",
                        ).items
                        if p.metadata
                    }
                    if pod_name not in current_pods and ready >= desired:
                        break
                    await asyncio.sleep(2.0)
                else:
                    return RestartOutcome(
                        succeeded=False,
                        restarted_pods=restarted,
                        message=f"Timed out waiting for replacement of {pod_name} to become Ready",
                    )

            return RestartOutcome(
                succeeded=True,
                restarted_pods=restarted,
                message=f"Rolling pod restart completed ({len(restarted)} pod(s) recycled)",
            )
        except Exception as exc:
            return RestartOutcome(
                succeeded=False,
                restarted_pods=[],
                message=f"Kubernetes pod restart failed: {exc}",
            )


async def process_restart_action(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    queue: QueueClient,
    backend: PodRestartBackend,
    action_request_id: uuid.UUID,
    timeout_seconds: float = 180.0,
) -> bool:
    now = datetime.now(tz=UTC)
    async with session_factory() as sess:
        claim = cast(
            CursorResult[Any],
            await sess.execute(
                update(ActionRequest)
                .where(
                    ActionRequest.id == action_request_id,
                    ActionRequest.action == "restart",
                    ActionRequest.status == "approved",
                )
                .values(status="executing", claimed_at=now)
            ),
        )
        if (claim.rowcount or 0) == 0:
            await sess.rollback()
            return False

        action = await sess.get(ActionRequest, action_request_id)
        assert action is not None
        incident = await sess.get(Incident, action.incident_id)
        assert incident is not None
        service = action.service or ""

        ev_exec = await append_incident_event(
            sess,
            incident_id=incident.id,
            event_type=EventType.ACTION_EXECUTING.value,
            actor="restart-executor",
            payload={"action_request_id": str(action.id), "action": "restart", "service": service},
        )
        await sess.commit()
    await notify_local_listeners(incident.id, ev_exec.seq)

    outcome = await backend.rolling_restart(service, timeout_seconds=timeout_seconds)

    notifications: list[tuple] = []
    async with session_factory() as sess:
        db_action = await sess.get(ActionRequest, action_request_id)
        db_inc = await sess.get(Incident, incident.id)
        assert db_action is not None and db_inc is not None
        db_action.executed_at = datetime.now(tz=UTC)
        db_action.result = outcome.to_dict()

        if outcome.succeeded:
            db_action.status = "succeeded"
            ev_ok = await append_incident_event(
                sess,
                incident_id=db_inc.id,
                event_type=EventType.ACTION_SUCCEEDED.value,
                actor="restart-executor",
                payload={"action_request_id": str(db_action.id), **outcome.to_dict()},
            )
            notifications.append((db_inc.id, ev_ok.seq))
        else:
            db_action.status = "failed"
            db_inc.status = IncidentStatus.AWAITING_DECISION.value
            ev_fail = await append_incident_event(
                sess,
                incident_id=db_inc.id,
                event_type=EventType.ACTION_FAILED.value,
                actor="restart-executor",
                payload={"action_request_id": str(db_action.id), **outcome.to_dict()},
            )
            ev_st = await append_incident_event(
                sess,
                incident_id=db_inc.id,
                event_type=EventType.INCIDENT_STATUS_CHANGED.value,
                actor="restart-executor",
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


def create_app(settings: SentinelSettings | None = None) -> FastAPI:
    cfg = settings or SentinelSettings()
    engine = make_engine(cfg.database_url)
    session_factory = make_session_factory(engine)
    queue = create_queue_client(cfg.rabbitmq_url, service="restart-executor")
    backend = KubernetesPodRestartBackend()
    stop_event = asyncio.Event()
    consumer_task: asyncio.Task[None] | None = None

    async def handle_action(msg: QueueMessage) -> None:
        if msg.payload.get("action") != "restart":
            if msg.payload.get("action") == "rollback":
                await queue.nack(msg, requeue=True, count_attempt=False)
                await asyncio.sleep(0.5)
                return
            await queue.ack(msg)
            return
        act_id = uuid.UUID(str(msg.payload["action_request_id"]))
        await process_restart_action(
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

    app = FastAPI(title="Sentinel Restart Executor (FR-AG-08)", version="1.0.0", lifespan=lifespan)
    setup_telemetry(app, service="restart-executor", project="sentinel", version="1.0.0")

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
