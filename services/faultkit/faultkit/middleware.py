"""Silent ASGI request-path middleware for latency and error_rate faults (FR-CH-01, FR-CH-03).

Raises ordinary, realistic runtime exceptions on error_rate so the host service's normal error
handler logs realistic symptoms with ZERO mention of faults, chaos, or injection.
"""

from __future__ import annotations

import asyncio
import random

from starlette.types import ASGIApp, Receive, Scope, Send

from faultkit.client import FaultStateStore

_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})

_REALISTIC_ERRORS: dict[str, tuple[tuple[type[Exception], str], ...]] = {
    "orders": (
        (RuntimeError, "order persistence transaction aborted: serialization failure"),
        (RuntimeError, "order state transition failed: unexpected empty cart checksum"),
        (ConnectionError, "upstream checkout session socket reset by peer"),
    ),
    "payments": (
        (RuntimeError, "card processor gateway returned malformed settlement frame"),
        (TimeoutError, "acquirer settlement channel read timed out after 3000ms"),
        (ConnectionError, "payment clearing house TLS handshake reset"),
    ),
}


async def apply_request_faults(store: FaultStateStore, rng: random.Random | None = None) -> None:
    """Apply active latency and error_rate faults inside the request execution path."""
    gen = rng or random.Random()
    for fault in store.active_faults():
        if fault.fault_type == "latency":
            share = float(fault.intensity.get("share", 1.0))
            delay_ms = float(fault.intensity.get("delay_ms", 500.0))
            if delay_ms > 0 and gen.random() < share:
                await asyncio.sleep(delay_ms / 1000.0)
        elif fault.fault_type == "error_rate":
            share = float(fault.intensity.get("share", 0.25))
            if gen.random() < share:
                pool = _REALISTIC_ERRORS.get(
                    store.service,
                    ((RuntimeError, "internal request processing failure"),),
                )
                exc_cls, msg = gen.choice(pool)
                raise exc_cls(msg)


class FaultMiddleware:
    """Pure ASGI middleware applying latency and error_rate faults transparently."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: FaultStateStore,
        rng: random.Random | None = None,
    ) -> None:
        self.app = app
        self.store = store
        self._rng = rng or random.Random()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        await apply_request_faults(self.store, self._rng)
        await self.app(scope, receive, send)
