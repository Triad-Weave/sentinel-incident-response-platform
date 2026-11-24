"""Isolated chaos-controller service (namespace: sentinel-chaos, DB: chaos).

- Operator endpoints (`POST /v1/faults`, `DELETE /v1/faults/{id}`, `DELETE /v1/faults`,
  `GET /v1/ledger`, `POST /v1/ledger/release`) require the operator bearer token.
- Service read endpoint (`GET /v1/faults/active?service=<svc>`) requires the per-service read token
  and returns ONLY minimal active runtime fault parameters (no fault_id, no scenario_id, no ledger!).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from platform_telemetry import setup_telemetry
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from chaos_controller.config import ChaosSettings
from chaos_controller.models import ChaosBase, FaultLedgerRow

ServiceName = Literal["orders", "payments"]
FaultType = Literal[
    "latency",
    "error_rate",
    "db_exhaustion",
    "bad_release",
    "memory_leak",
    "cpu_saturation",
]


class CreateFaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: FaultType
    service: ServiceName
    intensity: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: int = Field(default=600, ge=10, le=7200)
    actor: str = Field(default="operator", min_length=1, max_length=128)
    scenario_id: str | None = Field(default=None, max_length=128)
    expected_action: str | None = Field(default=None, max_length=32)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def create_app(settings: ChaosSettings | None = None, engine: AsyncEngine | None = None) -> FastAPI:
    cfg = settings or ChaosSettings()
    db_engine = engine or create_async_engine(cfg.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with db_engine.begin() as conn:
            await conn.run_sync(ChaosBase.metadata.create_all)
        yield
        if engine is None:
            await db_engine.dispose()

    app = FastAPI(title="Sentinel Chaos Controller", version="1.0.0", lifespan=lifespan)
    setup_telemetry(
        app,
        service="chaos-controller",
        project="sentinel",
        version="1.0.0",
        log_level=cfg.log_level,
    )

    def require_operator(authorization: str | None = Header(default=None)) -> None:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token != cfg.operator_token:
            raise HTTPException(status_code=403, detail="Operator token required")

    def require_service_token(
        service: ServiceName = Query(...),
        authorization: str | None = Header(default=None),
    ) -> ServiceName:
        token = (authorization or "").removeprefix("Bearer ").strip()
        expected = cfg.orders_read_token if service == "orders" else cfg.payments_read_token
        if token != expected:
            raise HTTPException(status_code=403, detail="Invalid service read token")
        return service

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.post("/v1/faults", status_code=201, dependencies=[Depends(require_operator)])
    async def create_fault(req: CreateFaultRequest) -> dict[str, Any]:
        now = datetime.now(tz=UTC)
        row = FaultLedgerRow(
            fault_type=req.type,
            service=req.service,
            intensity=req.intensity,
            started_at=now,
            expires_at=now + timedelta(seconds=req.duration_seconds),
            actor=req.actor,
            scenario_id=req.scenario_id,
            expected_action=req.expected_action,
        )
        async with session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _serialize_ledger(row)

    @app.get("/v1/faults/active")
    async def get_active_faults(
        service: ServiceName = Depends(require_service_token),
    ) -> dict[str, Any]:
        """Minimal read endpoint polled by faultkit every 2s. Never returns IDs or scenario info."""
        now = datetime.now(tz=UTC)
        async with session_factory() as session:
            res = await session.execute(
                select(FaultLedgerRow).where(
                    FaultLedgerRow.service == service,
                    FaultLedgerRow.cleared_at.is_(None),
                    FaultLedgerRow.fault_type != "bad_release",
                )
            )
            rows = res.scalars().all()

        active = [
            {
                "type": r.fault_type,
                "intensity": r.intensity,
                "started_at": _as_utc(r.started_at).isoformat(),
                "expires_at": _as_utc(r.expires_at).isoformat(),
            }
            for r in rows
            if _as_utc(r.expires_at) > now
        ]
        return {"service": service, "faults": active}

    @app.delete("/v1/faults/{fault_id}", dependencies=[Depends(require_operator)])
    async def clear_fault(fault_id: uuid.UUID) -> dict[str, Any]:
        now = datetime.now(tz=UTC)
        async with session_factory() as session:
            row = await session.get(FaultLedgerRow, fault_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Fault not found")
            if row.cleared_at is None:
                row.cleared_at = now
                await session.commit()
                await session.refresh(row)
        return _serialize_ledger(row)

    @app.delete("/v1/faults", dependencies=[Depends(require_operator)])
    async def clear_all_faults(service: ServiceName | None = None) -> dict[str, Any]:
        now = datetime.now(tz=UTC)
        async with session_factory() as session:
            stmt = select(FaultLedgerRow).where(FaultLedgerRow.cleared_at.is_(None))
            if service:
                stmt = stmt.where(FaultLedgerRow.service == service)
            rows = (await session.execute(stmt)).scalars().all()
            count = 0
            for r in rows:
                r.cleared_at = now
                count += 1
            await session.commit()
        return {"cleared": count}

    @app.get("/v1/ledger", dependencies=[Depends(require_operator)])
    async def list_ledger(
        active_only: bool = False,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        now = datetime.now(tz=UTC)
        async with session_factory() as session:
            stmt = select(FaultLedgerRow).order_by(FaultLedgerRow.started_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
        if active_only:
            rows = [r for r in rows if r.cleared_at is None and _as_utc(r.expires_at) > now]
        return {"items": [_serialize_ledger(r) for r in rows]}

    return app


def _serialize_ledger(row: FaultLedgerRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "type": row.fault_type,
        "service": row.service,
        "intensity": row.intensity,
        "started_at": _as_utc(row.started_at).isoformat(),
        "expires_at": _as_utc(row.expires_at).isoformat(),
        "cleared_at": _as_utc(row.cleared_at).isoformat() if row.cleared_at else None,
        "actor": row.actor,
        "scenario_id": row.scenario_id,
        "expected_action": row.expected_action,
    }


app = create_app()
