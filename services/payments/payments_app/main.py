"""Payments FastAPI service (namespace: shop).

- POST /authorize: simulated card authorization (~2% business declines); logs ONLY last 4 digits
- Emits payment_failures_total{reason=declined|error|timeout} + PB-09 telemetry + DB pool metrics
- Logs a realistic canary leaked-config line at startup and every 5 minutes (NFR-05 verification)
- /healthz and /readyz (DB within 1 second)
- Runs silent faultkit middleware + resource fault manager
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from faultkit import FaultStateStore, ResourceFaultManager, apply_request_faults
from orders_app.db import PoolAcquireTimeoutError
from platform_telemetry import base_labels, setup_telemetry
from platform_telemetry.metrics import COMMON_LABELS
from prometheus_client import Counter
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from payments_app.config import PaymentsSettings
from payments_app.db import AuthorizationRow, PaymentsPool

PAYMENT_FAILURES_TOTAL = Counter(
    "payment_failures_total",
    "Payment authorization failures by reason (declined, error, timeout)",
    labelnames=(*COMMON_LABELS, "reason"),
)


class AuthorizeRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(ge=1, le=100_000_000)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    card_number: str = Field(min_length=12, max_length=19)


def _should_business_decline(order_id: str, decline_rate: float) -> bool:
    digest = hashlib.sha256(order_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return bucket < decline_rate


def create_app(
    settings: PaymentsSettings | None = None,
    *,
    engine: AsyncEngine | None = None,
    fault_store: FaultStateStore | None = None,
) -> FastAPI:
    cfg = settings or PaymentsSettings()
    pool = PaymentsPool(
        database_url=cfg.database_url,
        pool_size=cfg.pool_size,
        pool_timeout_s=cfg.pool_timeout_seconds,
        engine=engine,
    )
    store = fault_store or FaultStateStore(
        service="payments",
        controller_url=cfg.chaos_controller_url,
        read_token=cfg.chaos_read_token,
    )
    resource_mgr = ResourceFaultManager(
        store=store,
        acquire_db_conn=pool.acquire_raw,
        release_db_conn=pool.release_raw,
        default_pool_size=cfg.pool_size,
    )
    labels = base_labels(project="sentinel", service="payments")
    canary_stop = asyncio.Event()
    canary_task: asyncio.Task[None] | None = None

    app = FastAPI(title="Sentinel Shop — Payments", version=cfg.version)
    logger = setup_telemetry(
        app,
        service="payments",
        project="sentinel",
        version=cfg.version,
        log_level=cfg.log_level,
    )

    def _emit_canary_log() -> None:
        # NFR-05 canary: realistic leaked config line that MUST be redacted before any LLM call
        logger.warning(
            "gateway_config_loaded",
            extra={
                "config_summary": (
                    f"acquirer_endpoint=https://acquirer.internal "
                    f"password={cfg.canary_password} "
                    f"Authorization=Bearer {cfg.canary_bearer_token}"
                )
            },
        )

    async def _canary_loop() -> None:
        _emit_canary_log()
        while not canary_stop.is_set():
            try:
                await asyncio.wait_for(canary_stop.wait(), timeout=cfg.canary_interval_seconds)
            except TimeoutError:
                _emit_canary_log()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal canary_task
        await pool.init_schema()
        store.start()
        resource_mgr.start()
        canary_stop.clear()
        canary_task = asyncio.create_task(_canary_loop())
        yield
        canary_stop.set()
        if canary_task is not None:
            canary_task.cancel()
            try:
                await canary_task
            except asyncio.CancelledError:
                pass
        await resource_mgr.stop()
        await store.stop()
        await pool.dispose()

    app.router.lifespan_context = lifespan

    async def _fault_gate() -> None:
        await apply_request_faults(store)

    app.state.pool = pool
    app.state.fault_store = store
    app.state.resource_mgr = resource_mgr

    @app.exception_handler(PoolAcquireTimeoutError)
    async def pool_timeout_handler(_request: Request, exc: PoolAcquireTimeoutError) -> JSONResponse:
        PAYMENT_FAILURES_TOTAL.labels(**labels, reason="timeout").inc()
        logger.error(
            "db_pool_acquire_timeout",
            extra={"error.type": type(exc).__name__, "error.message": str(exc)},
        )
        return JSONResponse(status_code=503, content={"detail": "Payments database unavailable"})

    @app.exception_handler(TimeoutError)
    async def timeout_handler(_request: Request, exc: TimeoutError) -> JSONResponse:
        PAYMENT_FAILURES_TOTAL.labels(**labels, reason="timeout").inc()
        logger.error(
            "payment_authorization_timeout",
            extra={"error.type": type(exc).__name__, "error.message": str(exc)},
        )
        return JSONResponse(status_code=504, content={"detail": "Payment gateway timeout"})

    @app.exception_handler(ConnectionError)
    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        PAYMENT_FAILURES_TOTAL.labels(**labels, reason="error").inc()
        logger.error(
            "payment_authorization_error",
            extra={"error.type": type(exc).__name__, "error.message": str(exc)},
        )
        return JSONResponse(status_code=500, content={"detail": "Payment processing error"})

    @app.exception_handler(Exception)
    async def unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
        PAYMENT_FAILURES_TOTAL.labels(**labels, reason="error").inc()
        logger.error(
            "payment_authorization_error",
            extra={"error.type": type(exc).__name__, "error.message": str(exc)},
        )
        return JSONResponse(status_code=500, content={"detail": "Payment processing error"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "payments", "version": cfg.version}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        try:
            async with asyncio.timeout(1.0):
                async with pool.session() as session:
                    await session.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception as exc:
            logger.error(
                "readiness_check_failed",
                extra={"error.type": type(exc).__name__, "error.message": str(exc)},
            )
            raise HTTPException(status_code=503, detail="Payments database not ready") from exc

    @app.post("/authorize", dependencies=[Depends(_fault_gate)])
    async def authorize_payment(req: AuthorizeRequest) -> dict[str, Any]:
        # Strictly log ONLY last 4 digits (NFR-05); card 4000000000000002 is always declined (5.2)
        card_last4 = req.card_number[-4:]
        declined = (req.card_number == "4000000000000002") or _should_business_decline(
            req.order_id, cfg.decline_rate
        )
        auth_status = "declined" if declined else "approved"
        reason = "insufficient_funds" if declined else None

        row = AuthorizationRow(
            order_id=req.order_id,
            amount_cents=req.amount_cents,
            currency=req.currency,
            card_last4=card_last4,
            status=auth_status,
            reason=reason,
        )
        async with pool.session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)

        if declined:
            PAYMENT_FAILURES_TOTAL.labels(**labels, reason="declined").inc()
            logger.info(
                "payment_declined",
                extra={"order_id": req.order_id, "card_last4": card_last4, "reason": reason},
            )
        else:
            logger.info(
                "payment_authorized",
                extra={"order_id": req.order_id, "card_last4": card_last4},
            )

        return {
            "authorization_id": str(row.id),
            "order_id": req.order_id,
            "approved": not declined,
            "status": auth_status,
            "reason": reason,
            "card_last4": card_last4,
        }

    return app


app = create_app()
