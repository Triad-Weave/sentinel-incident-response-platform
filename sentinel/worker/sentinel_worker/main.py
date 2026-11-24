"""Sentinel Worker entrypoint: consumes `sentinel.investigations`, runs watchdog, exposes /healthz, /readyz, /metrics."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from platform_telemetry import setup_telemetry
from sentinel_common.config import SentinelSettings
from sentinel_common.db import init_sentinel_schema, make_engine, make_session_factory
from sentinel_common.queue import QUEUE_INVESTIGATIONS, QueueMessage, create_queue_client
from sqlalchemy import text

from sentinel_worker.agent import AgentLimits, InvestigationAgent
from sentinel_worker.llm_client import GatewayLlmClient
from sentinel_worker.mcp_client import HttpMcpToolClient
from sentinel_worker.postmortem import generate_postmortem
from sentinel_worker.verifier import verify_action_mitigation
from sentinel_worker.watchdog import sweep_auto_resolve_incidents, sweep_stuck_investigations


def create_app(settings: SentinelSettings | None = None) -> FastAPI:
    cfg = settings or SentinelSettings()
    engine = make_engine(cfg.database_url)
    session_factory = make_session_factory(engine)
    queue = create_queue_client(cfg.rabbitmq_url, service="sentinel-worker")
    mcp_client = HttpMcpToolClient(base_url=cfg.mcp_url, token=cfg.mcp_token)
    llm_client = GatewayLlmClient(base_url=cfg.llm_gateway_url, api_key=cfg.llm_gateway_api_key)
    agent = InvestigationAgent(
        session_factory=session_factory,
        mcp_client=mcp_client,
        llm_client=llm_client,
        model=cfg.agent_model,
        limits=AgentLimits(
            max_tool_calls=cfg.agent_max_tool_calls,
            max_wall_seconds=cfg.agent_max_wall_seconds,
            max_input_tokens=cfg.agent_max_input_tokens,
            max_output_tokens=cfg.agent_max_output_tokens,
            max_cost_usd=cfg.agent_max_cost_usd,
        ),
        enable_restart=cfg.enable_restart_action,
    )

    stop_event = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def handle_job(msg: QueueMessage) -> None:
        job_type = str(msg.payload.get("job", "investigate"))
        if job_type == "investigate":
            inc_id = uuid.UUID(str(msg.payload["incident_id"]))
            await agent.run_investigation(inc_id)
        elif job_type == "verify":
            act_id = uuid.UUID(str(msg.payload["action_request_id"]))
            await verify_action_mitigation(
                session_factory=session_factory,
                mcp_client=mcp_client,
                action_request_id=act_id,
            )
        elif job_type == "postmortem" and cfg.enable_postmortem:
            inc_id = uuid.UUID(str(msg.payload["incident_id"]))
            await generate_postmortem(
                session_factory=session_factory,
                llm_client=llm_client,
                incident_id=inc_id,
                model=cfg.agent_model,
            )
        # Ack ONLY after reaching a terminal state (NFR-06)
        await queue.ack(msg)

    async def watchdog_loop() -> None:
        while not stop_event.is_set():
            try:
                await sweep_stuck_investigations(session_factory)
                await sweep_auto_resolve_incidents(session_factory, queue=queue)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=15.0)
            except TimeoutError:
                continue

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await init_sentinel_schema(engine, skip_ddl_on_postgres=True)
        stop_event.clear()
        tasks.append(
            asyncio.create_task(
                queue.consume(QUEUE_INVESTIGATIONS, handle_job, stop_event=stop_event)
            )
        )
        tasks.append(asyncio.create_task(watchdog_loop()))
        yield
        stop_event.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.close()
        await engine.dispose()

    app = FastAPI(title="Sentinel Investigation Worker", version="1.0.0", lifespan=lifespan)
    setup_telemetry(
        app, service="sentinel-worker", project="sentinel", version="1.0.0", log_level=cfg.log_level
    )

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
