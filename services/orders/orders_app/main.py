"""Orders FastAPI service (namespace: shop).

- POST /orders, GET /orders/{id}, GET /orders (FR-TS-01)
- Calls Payments POST /authorize for every order, forwarding X-Request-ID and traceparent
- /healthz (liveness) and /readyz (checks ONLY own database within 1s, NEVER Payments — FR-TS-03)
- Emits PB-09 telemetry + orders_created_total + DB pool metrics (FR-OB-01)
- Runs silent faultkit middleware + resource fault manager (FR-CH-01, ADR-001)
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from faultkit import FaultStateStore, ResourceFaultManager, apply_request_faults
from platform_telemetry import base_labels, instrumented_request, setup_telemetry
from platform_telemetry.metrics import COMMON_LABELS
from prometheus_client import Counter
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from orders_app.config import OrdersSettings
from orders_app.db import InstrumentedPool, OrderRow, PoolAcquireTimeoutError

ORDERS_CREATED_TOTAL = Counter(
    "orders_created_total",
    "Total orders created or failed (FR-OB-01)",
    labelnames=(*COMMON_LABELS, "status"),
)


class LineItem(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=1, le=100)
    unit_price_cents: int = Field(ge=1, le=10_000_000)


class CreateOrderRequest(BaseModel):
    customer_id: str = Field(min_length=1, max_length=64)
    items: list[LineItem] = Field(min_length=1, max_length=20)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    card_number: str = Field(default="4242424242424242", min_length=12, max_length=19)


def calculate_order_total(items: list[LineItem]) -> int:
    """Compute order subtotal plus flat 8% tax in cents."""
    subtotal = sum(item.qty * item.unit_price_cents for item in items)
    tax = round(subtotal * 0.08)
    return subtotal + tax


def create_app(
    settings: OrdersSettings | None = None,
    *,
    engine: AsyncEngine | None = None,
    payments_client: httpx.AsyncClient | None = None,
    fault_store: FaultStateStore | None = None,
) -> FastAPI:
    cfg = settings or OrdersSettings()
    pool = InstrumentedPool(
        database_url=cfg.database_url,
        pool_size=cfg.pool_size,
        pool_timeout_s=cfg.pool_timeout_seconds,
        service="orders",
        project="sentinel",
        engine=engine,
    )
    store = fault_store or FaultStateStore(
        service="orders",
        controller_url=cfg.chaos_controller_url,
        read_token=cfg.chaos_read_token,
    )
    resource_mgr = ResourceFaultManager(
        store=store,
        acquire_db_conn=pool.acquire_raw,
        release_db_conn=pool.release_raw,
        default_pool_size=cfg.pool_size,
    )
    owns_client = payments_client is None
    http_client = payments_client or httpx.AsyncClient(
        base_url=cfg.payments_url,
        timeout=cfg.payments_timeout_seconds,
    )
    labels = base_labels(project="sentinel", service="orders")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await pool.init_schema()
        store.start()
        resource_mgr.start()
        yield
        await resource_mgr.stop()
        await store.stop()
        if owns_client:
            await http_client.aclose()
        await pool.dispose()

    app = FastAPI(title="Sentinel Shop — Orders", version=cfg.version, lifespan=lifespan)
    logger = setup_telemetry(
        app,
        service="orders",
        project="sentinel",
        version=cfg.version,
        log_level=cfg.log_level,
    )

    async def _fault_gate() -> None:
        await apply_request_faults(store)

    app.state.pool = pool
    app.state.fault_store = store
    app.state.resource_mgr = resource_mgr

    @app.exception_handler(PoolAcquireTimeoutError)
    async def pool_timeout_handler(_request: Request, exc: PoolAcquireTimeoutError) -> JSONResponse:
        logger.error(
            "db_pool_acquire_timeout",
            extra={"error.type": type(exc).__name__, "error.message": str(exc)},
        )
        return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})

    @app.exception_handler(ConnectionError)
    @app.exception_handler(RuntimeError)
    @app.exception_handler(KeyError)
    async def runtime_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        ORDERS_CREATED_TOTAL.labels(**labels, status="failed").inc()
        logger.error(
            "unhandled_orders_error",
            extra={"error.type": type(exc).__name__, "error.message": str(exc)},
        )
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_orders_error",
            extra={"error.type": type(exc).__name__, "error.message": str(exc)},
        )
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "orders", "version": cfg.version}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Checks ONLY Orders DB within 1 second; NEVER calls Payments (FR-TS-03)."""
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
            raise HTTPException(status_code=503, detail="Orders database not ready") from exc

    @app.post("/orders", status_code=201, response_model=None, dependencies=[Depends(_fault_gate)])
    async def create_order(req: CreateOrderRequest) -> dict[str, Any] | JSONResponse:
        total_cents = calculate_order_total(req.items)
        order_id = uuid.uuid4()

        # Call Payments for every order (FR-TS-01), forwarding X-Request-ID & traceparent
        try:
            pay_resp = await instrumented_request(
                http_client,
                "POST",
                "/authorize",
                project="sentinel",
                service="orders",
                server_address="payments",
                json={
                    "order_id": str(order_id),
                    "amount_cents": total_cents,
                    "currency": req.currency,
                    "card_number": req.card_number,
                },
            )
        except httpx.TimeoutException as exc:
            ORDERS_CREATED_TOTAL.labels(**labels, status="failed").inc()
            logger.error(
                "payments_authorization_timeout",
                extra={"error.type": "TimeoutException", "error.message": str(exc)},
            )
            raise HTTPException(status_code=502, detail="Payments authorization timed out") from exc
        except httpx.HTTPError as exc:
            ORDERS_CREATED_TOTAL.labels(**labels, status="failed").inc()
            logger.error(
                "payments_authorization_network_error",
                extra={"error.type": type(exc).__name__, "error.message": str(exc)},
            )
            raise HTTPException(status_code=502, detail="Payments service unreachable") from exc

        if pay_resp.status_code >= 500:
            ORDERS_CREATED_TOTAL.labels(**labels, status="failed").inc()
            logger.error(
                "payments_upstream_5xx",
                extra={
                    "error.type": "UpstreamError",
                    "error.message": f"payments returned HTTP {pay_resp.status_code}",
                },
            )
            raise HTTPException(status_code=502, detail="Payment authorization failed upstream")

        pay_data = pay_resp.json()
        approved = bool(pay_data.get("approved"))
        order_status = "paid" if approved else "declined"

        row = OrderRow(
            id=order_id,
            customer_id=req.customer_id,
            items=[i.model_dump() for i in req.items],
            total_cents=total_cents,
            currency=req.currency,
            status=order_status,
            payment_auth_id=pay_data.get("authorization_id"),
        )
        try:
            async with pool.session() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
        except PoolAcquireTimeoutError:
            ORDERS_CREATED_TOTAL.labels(**labels, status="failed").inc()
            raise

        metric_status = "created" if approved else "failed"
        ORDERS_CREATED_TOTAL.labels(**labels, status=metric_status).inc()
        payload = _serialize_order(row)
        if not approved:
            return JSONResponse(status_code=402, content=payload)
        return payload

    @app.get("/orders/{order_id}", dependencies=[Depends(_fault_gate)])
    async def get_order(order_id: uuid.UUID) -> dict[str, Any]:
        async with pool.session() as session:
            row = await session.get(OrderRow, order_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Order not found")
        return _serialize_order(row)

    @app.get("/orders", dependencies=[Depends(_fault_gate)])
    async def list_orders(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        async with pool.session() as session:
            res = await session.execute(
                select(OrderRow).order_by(OrderRow.created_at.desc()).limit(limit)
            )
            rows = res.scalars().all()
        return {"orders": [_serialize_order(r) for r in rows]}

    return app


def _serialize_order(row: OrderRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "customer_id": row.customer_id,
        "items": row.items,
        "total_cents": int(row.total_cents),
        "currency": row.currency,
        "status": row.status,
        "payment_auth_id": row.payment_auth_id,
        "created_at": row.created_at.isoformat(),
    }


app = create_app()
