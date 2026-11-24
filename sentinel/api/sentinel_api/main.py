"""Sentinel API FastAPI application (namespace: sentinel)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from platform_telemetry import setup_telemetry
from sentinel_common.config import SentinelSettings
from sentinel_common.db import init_sentinel_schema, make_engine, make_session_factory
from sentinel_common.queue import QueueClient, create_queue_client
from sentinel_worker.llm_client import GatewayLlmClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sentinel_api.routes import actions, alerts, auth, incidents
from sentinel_api.seed import seed_default_users
from sentinel_api.sse import IncidentEventBroker


def create_app(
    settings: SentinelSettings | None = None,
    *,
    engine: AsyncEngine | None = None,
    queue: QueueClient | None = None,
    llm_client: GatewayLlmClient | None = None,
    auto_seed: bool = True,
) -> FastAPI:
    cfg = settings or SentinelSettings()
    owns_engine = engine is None
    db_engine = engine or make_engine(cfg.database_url)
    session_factory = make_session_factory(db_engine)
    owns_queue = queue is None
    queue_client = queue or create_queue_client(cfg.rabbitmq_url, service="sentinel-api")
    broker = IncidentEventBroker(cfg.database_url, session_factory)
    resolved_llm = llm_client or GatewayLlmClient(
        base_url=cfg.llm_gateway_url, api_key=cfg.llm_gateway_api_key
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await init_sentinel_schema(db_engine)
        if auto_seed:
            await seed_default_users(session_factory)
        await broker.start()
        yield
        await broker.stop()
        if owns_queue:
            await queue_client.close()
        if owns_engine:
            await db_engine.dispose()

    app = FastAPI(title="Sentinel Incident API", version="1.0.0", lifespan=lifespan)
    setup_telemetry(
        app, service="sentinel-api", project="sentinel", version="1.0.0", log_level=cfg.log_level
    )

    app.state.settings = cfg
    app.state.engine = db_engine
    app.state.session_factory = session_factory
    app.state.queue = queue_client
    app.state.broker = broker
    app.state.llm_client = resolved_llm

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        async with session_factory() as sess:
            await sess.execute(text("SELECT 1"))
        return {"status": "ready"}

    app.include_router(alerts.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(incidents.router, prefix="/api/v1")
    app.include_router(actions.router, prefix="/api/v1")

    static_dir = Path("/app/console_dist")
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="console")

    return app


app = create_app()
