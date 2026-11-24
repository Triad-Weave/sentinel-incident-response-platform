"""faultkit — silent in-process fault injection middleware (ADR-001)."""

from faultkit.client import ActiveFault, FaultStateStore
from faultkit.middleware import FaultMiddleware, apply_request_faults
from faultkit.resources import ResourceFaultManager

__all__ = [
    "ActiveFault",
    "FaultStateStore",
    "FaultMiddleware",
    "ResourceFaultManager",
    "apply_request_faults",
]
