"""Structured JSON logging conforming to the shared platform telemetry contract (PB-09).

Contract fields on every log line:
  ts, level, msg, service, project, request_id, trace_id
Optional fields:
  route, status, duration_ms, error.type, error.message, version, plus extra keys.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class PlatformJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON matching the PB-09 contract."""

    def __init__(self, *, service: str, project: str, version: str = "0.0.0") -> None:
        super().__init__()
        self.service = service
        self.project = project
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            "service": self.service,
            "project": self.project,
            "version": self.version,
            "request_id": getattr(record, "request_id", None) or request_id_var.get() or None,
            "trace_id": getattr(record, "trace_id", None) or trace_id_var.get() or None,
        }

        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_val, _ = record.exc_info
            payload["error.type"] = exc_type.__name__
            payload["error.message"] = str(exc_val)

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            if key in ("request_id", "trace_id") and payload.get(key):
                continue
            payload[key] = value

        # Drop None values for optional fields while keeping core contract keys present
        core_keys = {"ts", "level", "msg", "service", "project", "request_id", "trace_id"}
        cleaned = {k: v for k, v in payload.items() if v is not None or k in core_keys}
        return json.dumps(cleaned, default=str, separators=(",", ":"))


def configure_logging(
    *,
    service: str,
    project: str,
    version: str = "0.0.0",
    level: str = "INFO",
) -> logging.Logger:
    """Configure root JSON logging for a platform service."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(PlatformJsonFormatter(service=service, project=project, version=version))
    root.handlers = [handler]

    # Silence noisy loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return logging.getLogger(service)
