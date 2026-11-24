"""Platform telemetry setup for FastAPI services (PB-09, PB-11)."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

from platform_telemetry.logging import configure_logging, request_id_var, trace_id_var
from platform_telemetry.metrics import (
    HTTP_CLIENT_DURATION,
    HTTP_SERVER_DURATION,
    QUEUE_BACKLOG,
    QUEUE_DEAD_LETTERS,
    QUEUE_MESSAGES_CONSUMED,
    QUEUE_MESSAGES_PUBLISHED,
    base_labels,
    render_metrics,
)
from platform_telemetry.tracing import child_traceparent, parse_or_create_trace_id, setup_tracing

__all__ = [
    "setup_telemetry",
    "instrumented_request",
    "propagation_headers",
    "extract_queue_context",
    "request_id_var",
    "trace_id_var",
    "HTTP_SERVER_DURATION",
    "HTTP_CLIENT_DURATION",
    "QUEUE_MESSAGES_PUBLISHED",
    "QUEUE_MESSAGES_CONSUMED",
    "QUEUE_BACKLOG",
    "QUEUE_DEAD_LETTERS",
    "base_labels",
]

_SKIP_METRICS_PATHS = frozenset({"/metrics", "/healthz", "/readyz"})


def propagation_headers() -> dict[str, str]:
    """Build outbound correlation headers (X-Request-ID + W3C traceparent).

    Used both for HTTP calls (Orders -> Payments) and queue message headers (Lumen/Folio/Sentinel).
    """
    rid = request_id_var.get() or uuid.uuid4().hex
    tid = trace_id_var.get()
    headers = {"X-Request-ID": rid}
    headers["traceparent"] = child_traceparent(tid)
    return headers


def extract_queue_context(headers: dict[str, Any] | None) -> tuple[str, str]:
    """Bind request_id and trace_id from incoming queue message headers into contextvars."""
    hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    rid = hdrs.get("x-request-id") or uuid.uuid4().hex
    tid, _ = parse_or_create_trace_id(hdrs)
    request_id_var.set(rid)
    trace_id_var.set(tid)
    return rid, tid


def setup_telemetry(
    app: FastAPI,
    *,
    service: str,
    project: str = "sentinel",
    version: str = "0.1.0",
    log_level: str = "INFO",
) -> logging.Logger:
    """Attach PB-09 JSON logging, HTTP RED metrics, traceparent propagation, and /metrics route."""
    logger = configure_logging(service=service, project=project, version=version, level=log_level)
    setup_tracing(service=service, project=project, version=version)
    labels = base_labels(project=project, service=service)

    @app.middleware("http")
    async def telemetry_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        trace_id, _ = parse_or_create_trace_id(request.headers)
        req_tok = request_id_var.set(req_id)
        tr_tok = trace_id_var.set(trace_id)
        start = time.perf_counter()
        status_code = 500
        err_type: str | None = None
        err_msg: str | None = None

        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = req_id
            response.headers["traceparent"] = child_traceparent(trace_id)
            return response
        except Exception as exc:
            err_type = type(exc).__name__
            err_msg = str(exc)
            raise
        finally:
            duration_s = time.perf_counter() - start
            route_obj = request.scope.get("route")
            route_path = getattr(route_obj, "path", request.url.path)
            if request.url.path not in _SKIP_METRICS_PATHS:
                HTTP_SERVER_DURATION.labels(
                    **labels,
                    http_route=route_path,
                    http_request_method=request.method,
                    http_response_status_code=str(status_code),
                ).observe(duration_s)
                log_extra: dict[str, Any] = {
                    "route": route_path,
                    "status": status_code,
                    "duration_ms": round(duration_s * 1000, 2),
                }
                if err_type:
                    log_extra["error.type"] = err_type
                    log_extra["error.message"] = err_msg
                if status_code >= 500:
                    logger.error("request_failed", extra=log_extra)
                elif status_code >= 400:
                    logger.warning("request_client_error", extra=log_extra)
                else:
                    logger.info("request_completed", extra=log_extra)
            request_id_var.reset(req_tok)
            trace_id_var.reset(tr_tok)

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    return logger


async def instrumented_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    project: str,
    service: str,
    server_address: str,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an outgoing HTTP call with PB-09 client duration histogram & header forwarding."""
    headers = dict(kwargs.pop("headers", None) or {})
    for k, v in propagation_headers().items():
        headers.setdefault(k, v)

    labels = base_labels(project=project, service=service)
    start = time.perf_counter()
    status_label = "error"
    try:
        resp = await client.request(method, url, headers=headers, **kwargs)
        status_label = str(resp.status_code)
        return resp
    except httpx.TimeoutException:
        status_label = "timeout"
        raise
    finally:
        HTTP_CLIENT_DURATION.labels(
            **labels,
            server_address=server_address,
            http_response_status_code=status_label,
        ).observe(time.perf_counter() - start)
