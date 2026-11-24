"""Strict enum-only PromQL & LogQL query builders, step widening, and onset detection (ADR-005).

All queries hard-code `namespace="shop"` and validate `service` strictly against `orders | payments`.
No free-form PromQL, LogQL, namespace, or selector can ever be injected by a model.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Literal

from sentinel_mcp.onset import compute_series_stats

__all__ = [
    "ALLOWED_SERVICES",
    "MAX_LOOKBACK_HOURS",
    "MAX_SERIES_POINTS",
    "METRIC_UNITS",
    "LogLevelEnum",
    "MetricEnum",
    "ServiceEnum",
    "ServiceOrAll",
    "build_logql",
    "build_promql",
    "clamp_end_time",
    "compute_series_stats",
    "compute_step_seconds",
]

ServiceEnum = Literal["orders", "payments"]
ServiceOrAll = Literal["orders", "payments", "all"]
LogLevelEnum = Literal["error", "warn", "info", "any"]
MetricEnum = Literal[
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
]

ALLOWED_SERVICES = frozenset({"orders", "payments"})

METRIC_UNITS: dict[str, str] = {
    "request_rate": "req/s",
    "error_ratio": "ratio",
    "latency_p50": "s",
    "latency_p95": "s",
    "latency_p99": "s",
    "orders_per_minute": "orders/min",
    "payment_failure_rate": "ratio",
    "upstream_error_ratio": "ratio",
    "upstream_latency_p95": "s",
    "db_pool_in_use_ratio": "ratio",
    "db_pool_wait_p95": "s",
    "pod_restarts": "count",
    "cpu_usage": "cores",
    "memory_usage": "bytes",
}

MAX_SERIES_POINTS = 30
MAX_LOOKBACK_HOURS = 6


def clamp_end_time(end_time: str | None, *, now: datetime | None = None) -> datetime:
    """Parse optional ISO 8601 end_time and clamp it within [now - 6h, now]."""
    ref = now or datetime.now(tz=UTC)
    if not end_time:
        return ref
    parsed = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    earliest = ref - timedelta(hours=MAX_LOOKBACK_HOURS)
    if parsed < earliest:
        return earliest
    if parsed > ref:
        return ref
    return parsed


def compute_step_seconds(window_minutes: int, requested_step_seconds: int) -> int:
    """Widen step so total points never exceed MAX_SERIES_POINTS (30)."""
    window_s = max(60, window_minutes * 60)
    min_step = math.ceil(window_s / MAX_SERIES_POINTS)
    return max(15, requested_step_seconds, min_step)


def build_promql(service: ServiceEnum, metric: MetricEnum) -> str:
    """Build a fixed-namespace (`namespace="shop"`) PromQL template for one allowed metric."""
    if service not in ALLOWED_SERVICES:
        raise ValueError(f"Unsupported service: {service}")

    base = f'namespace="shop",service="{service}"'
    templates: dict[str, str] = {
        "request_rate": (f"sum(rate(http_server_request_duration_seconds_count{{{base}}}[1m]))"),
        "error_ratio": (
            f'sum(rate(http_server_request_duration_seconds_count{{{base},http_response_status_code=~"5.."}}[1m])) '
            f"/ clamp_min(sum(rate(http_server_request_duration_seconds_count{{{base}}}[1m])), 0.001)"
        ),
        "latency_p50": (
            f"histogram_quantile(0.50, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{base}}}[1m])))"
        ),
        "latency_p95": (
            f"histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{base}}}[1m])))"
        ),
        "latency_p99": (
            f"histogram_quantile(0.99, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{base}}}[1m])))"
        ),
        "orders_per_minute": (f"sum(rate(orders_created_total{{{base}}}[1m])) * 60"),
        "payment_failure_rate": (
            f'sum(rate(payment_failures_total{{{base},reason!="declined"}}[1m])) '
            f"/ clamp_min(sum(rate(http_server_request_duration_seconds_count{{{base}}}[1m])), 0.001)"
        ),
        "upstream_error_ratio": (
            f'sum(rate(http_client_request_duration_seconds_count{{{base},http_response_status_code=~"5..|timeout|error"}}[1m])) '
            f"/ clamp_min(sum(rate(http_client_request_duration_seconds_count{{{base}}}[1m])), 0.001)"
        ),
        "upstream_latency_p95": (
            f"histogram_quantile(0.95, sum by (le) (rate(http_client_request_duration_seconds_bucket{{{base}}}[1m])))"
        ),
        "db_pool_in_use_ratio": (
            f"max(db_pool_connections_in_use{{{base}}}) / clamp_min(max(db_pool_size{{{base}}}), 1)"
        ),
        "db_pool_wait_p95": (
            f"histogram_quantile(0.95, sum by (le) (rate(db_pool_wait_seconds_bucket{{{base}}}[1m])))"
        ),
        "pod_restarts": (
            f'sum(increase(kube_pod_container_status_restarts_total{{namespace="shop",pod=~"{service}.*"}}[5m]))'
        ),
        "cpu_usage": (
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="shop",pod=~"{service}.*"}}[1m]))'
        ),
        "memory_usage": (
            f'sum(container_memory_working_set_bytes{{namespace="shop",pod=~"{service}.*"}})'
        ),
    }
    if metric not in templates:
        raise ValueError(f"Unsupported metric: {metric}")
    return templates[metric]


def build_logql(service: ServiceEnum, *, level: LogLevelEnum = "error", contains: str = "") -> str:
    """Build a strict, injection-safe LogQL selector pinned to `namespace="shop"`."""
    if service not in ALLOWED_SERVICES:
        raise ValueError(f"Unsupported service: {service}")
    if len(contains) > 100:
        raise ValueError("contains filter must be at most 100 characters")

    query = f'{{namespace="shop",service="{service}"}}'
    if level != "any":
        query += f' | json | level="{level}"'
    if contains:
        # Escape backslashes, quotes, and newlines so `contains` is always literal text
        escaped = contains.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        query += f' |= "{escaped}"'
    return query
