"""Contract Prometheus metrics (PB-09, PB-12, PB-13) shared across Sentinel, Lumen, and Folio."""

from __future__ import annotations

import os

# Use a dedicated default registry or the global REGISTRY so /metrics works seamlessly.
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

DEFAULT_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

COMMON_LABELS = ("project", "service", "namespace", "pod")

HTTP_SERVER_DURATION = Histogram(
    "http_server_request_duration_seconds",
    "HTTP server request duration in seconds (platform contract PB-09)",
    labelnames=(*COMMON_LABELS, "http_route", "http_request_method", "http_response_status_code"),
    buckets=DEFAULT_LATENCY_BUCKETS,
)

HTTP_CLIENT_DURATION = Histogram(
    "http_client_request_duration_seconds",
    "Outgoing HTTP client request duration in seconds (platform contract PB-09)",
    labelnames=(*COMMON_LABELS, "server_address", "http_response_status_code"),
    buckets=DEFAULT_LATENCY_BUCKETS,
)

# Shared queue_* contract metrics used by Sentinel, Lumen, and Folio (PB-12, PB-13, Folio FR-RL-03)
QUEUE_MESSAGES_PUBLISHED = Counter(
    "queue_messages_published_total",
    "Total messages published to a platform queue",
    labelnames=("project", "service", "queue"),
)

QUEUE_MESSAGES_CONSUMED = Counter(
    "queue_messages_consumed_total",
    "Total messages processed from a platform queue",
    labelnames=("project", "service", "queue", "status"),
)

QUEUE_BACKLOG = Gauge(
    "queue_backlog_messages",
    "Current queued messages awaiting consumption",
    labelnames=("project", "queue"),
)

QUEUE_DEAD_LETTERS = Gauge(
    "queue_dead_letter_messages",
    "Current messages parked in dead-letter queue",
    labelnames=("project", "queue"),
)


def base_labels(project: str, service: str) -> dict[str, str]:
    return {
        "project": project,
        "service": service,
        "namespace": os.getenv("POD_NAMESPACE", "shop"),
        "pod": os.getenv("HOSTNAME", f"{service}-local"),
    }


def render_metrics(registry: CollectorRegistry = REGISTRY) -> tuple[bytes, str]:
    return generate_latest(registry), CONTENT_TYPE_LATEST
