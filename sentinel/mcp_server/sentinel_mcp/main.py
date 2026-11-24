"""Sentinel MCP Server using the official `mcp` Python SDK (Streamable HTTP transport)."""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from platform_telemetry import setup_telemetry

from sentinel_mcp.backends import LiveClusterBackend, TelemetryBackend
from sentinel_mcp.tools import SentinelToolRegistry

READ_ONLY_ANNOTATIONS = ToolAnnotations(read_only_hint=True, idempotent_hint=True)


def build_fastmcp(registry: SentinelToolRegistry) -> MCPServer:
    mcp = MCPServer(name="sentinel-mcp")

    @mcp.tool(
        name="get_golden_signals",
        description="Return golden signals (request_rate, error_ratio, latency_p95, db_pool_in_use_ratio) with baseline, current, change_pct, and onset_at for orders and payments.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def get_golden_signals(window_minutes: int = 30, end_time: str | None = None) -> str:
        res = await registry.call_tool(
            "get_golden_signals", {"window_minutes": window_minutes, "end_time": end_time}
        )
        return json.dumps(res)

    @mcp.tool(
        name="query_metrics",
        description="Query one allow-listed Prometheus metric for orders or payments in namespace shop, downsampled to <=30 points with stats and onset_at.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def query_metrics(
        service: Literal["orders", "payments"],
        metric: Literal[
            "request_rate",
            "error_ratio",
            "latency_p50",
            "latency_p95",
            "latency_p99",
            "orders_per_minute",
            "payment_failure_rate",
            "upstream_error_ratio",
            "upstream_latency_p95",
            "db_pool_in_use_ratio",
            "db_pool_wait_p95",
            "pod_restarts",
            "cpu_usage",
            "memory_usage",
        ],
        window_minutes: int = 30,
        step_seconds: int = 60,
        end_time: str | None = None,
    ) -> str:
        res = await registry.call_tool(
            "query_metrics",
            {
                "service": service,
                "metric": metric,
                "window_minutes": window_minutes,
                "step_seconds": step_seconds,
                "end_time": end_time,
            },
        )
        return json.dumps(res)

    @mcp.tool(
        name="search_logs",
        description="Search and template-cluster logs for orders or payments in namespace shop (redacted, bounded to max_groups).",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def search_logs(
        service: Literal["orders", "payments"],
        level: Literal["error", "warn", "info", "any"] = "error",
        contains: str = "",
        window_minutes: int = 15,
        max_groups: int = 10,
        end_time: str | None = None,
    ) -> str:
        res = await registry.call_tool(
            "search_logs",
            {
                "service": service,
                "level": level,
                "contains": contains,
                "window_minutes": window_minutes,
                "max_groups": max_groups,
                "end_time": end_time,
            },
        )
        return json.dumps(res)

    @mcp.tool(
        name="get_pod_status",
        description="Read Deployment and Pod status fields (never env vars, args, or annotations) for orders or payments.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def get_pod_status(service: Literal["orders", "payments"]) -> str:
        res = await registry.call_tool("get_pod_status", {"service": service})
        return json.dumps(res)

    @mcp.tool(
        name="get_k8s_events",
        description="Read deduplicated Kubernetes events in namespace shop.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def get_k8s_events(
        service: Literal["orders", "payments", "all"] = "all",
        type: Literal["Warning", "Normal", "all"] = "all",
        window_minutes: int = 30,
        end_time: str | None = None,
    ) -> str:
        res = await registry.call_tool(
            "get_k8s_events",
            {
                "service": service,
                "type": type,
                "window_minutes": window_minutes,
                "end_time": end_time,
            },
        )
        return json.dumps(res)

    @mcp.tool(
        name="list_recent_deploys",
        description="List recent deploys from the PB-07 deploy-history API for orders and/or payments.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def list_recent_deploys(
        service: Literal["orders", "payments", "all"] = "all",
        lookback_minutes: int = 120,
        limit: int = 5,
        end_time: str | None = None,
    ) -> str:
        res = await registry.call_tool(
            "list_recent_deploys",
            {
                "service": service,
                "lookback_minutes": lookback_minutes,
                "limit": limit,
                "end_time": end_time,
            },
        )
        return json.dumps(res)

    return mcp


def create_app(backend: TelemetryBackend | None = None) -> FastAPI:
    resolved_backend = backend or LiveClusterBackend(
        prometheus_url=os.getenv("PROMETHEUS_URL", "http://prometheus:9090"),
        loki_url=os.getenv("LOKI_URL", "http://loki:3100"),
        pb07_url=os.getenv("PB07_CONTROLLER_URL", "http://rollback-controller:8095"),
        deploy_history_token=os.getenv("DEPLOY_HISTORY_READ_TOKEN", "dev-deploy-history-token"),
    )
    registry = SentinelToolRegistry(resolved_backend)
    mcp_server = build_fastmcp(registry)
    expected_token = os.getenv("SENTINEL_MCP_TOKEN", "dev-mcp-token")

    app = FastAPI(title="Sentinel Read-Only MCP Server", version="1.0.0")
    setup_telemetry(app, service="sentinel-mcp", project="sentinel", version="1.0.0")
    app.state.registry = registry
    app.state.mcp = mcp_server

    def _is_valid_token(authorization: str | None) -> bool:
        if not expected_token:
            return True
        token = (authorization or "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(token, expected_token)

    def _check_token(authorization: str | None = Header(default=None)) -> None:
        if not _is_valid_token(authorization):
            raise HTTPException(status_code=403, detail="Invalid MCP token")

    @app.middleware("http")
    async def enforce_mcp_mount_auth(request: Request, call_next: Any) -> Response:
        if (request.url.path == "/mcp" or request.url.path.startswith("/mcp/")) and (
            not _is_valid_token(request.headers.get("authorization"))
        ):
            return JSONResponse(status_code=403, content={"detail": "Invalid MCP token"})
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/v1/tools/{tool_name}", dependencies=[Depends(_check_token)])
    async def invoke_tool_http(tool_name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Direct JSON-RPC/HTTP convenience endpoint sharing identical validation & redaction."""
        return await registry.call_tool(tool_name, body)

    # Mount official MCP Streamable HTTP endpoint at /mcp
    app.mount("/mcp", mcp_server.streamable_http_app())
    return app


app = create_app()
