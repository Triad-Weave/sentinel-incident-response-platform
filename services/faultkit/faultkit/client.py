"""Silent fault state poller for target services (ADR-001).

CRITICAL INVARIANTS (FR-CH-03):
- NEVER writes any log line, metric, or trace span.
- Polling httpx.AsyncClient is completely uninstrumented.
- If chaos-controller is unreachable, retains the last answer until `expires_at`, then fails safe
  (zero active faults).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class ActiveFault:
    fault_type: str
    intensity: dict[str, Any]
    started_at: datetime
    expires_at: datetime


class FaultStateStore:
    """In-memory active fault cache with silent background polling and fail-safe expiry."""

    def __init__(
        self,
        *,
        service: str,
        controller_url: str,
        read_token: str,
        poll_interval_s: float = 2.0,
        pod_started_at: datetime | None = None,
    ) -> None:
        self.service = service
        self.controller_url = controller_url.rstrip("/")
        self.read_token = read_token
        self.poll_interval_s = poll_interval_s
        self.pod_started_at = pod_started_at or datetime.now(tz=UTC)
        self._faults: list[ActiveFault] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None

    def set_faults_for_test(self, faults: list[ActiveFault]) -> None:
        self._faults = list(faults)

    def active_faults(self, now: datetime | None = None) -> list[ActiveFault]:
        current = now or datetime.now(tz=UTC)
        return [f for f in self._faults if f.expires_at > current]

    async def poll_once(self) -> None:
        if not self.controller_url:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=1.5)
        try:
            resp = await self._client.get(
                f"{self.controller_url}/v1/faults/active",
                params={"service": self.service},
                headers={"Authorization": f"Bearer {self.read_token}"},
            )
            if resp.status_code == 200:
                payload = resp.json()
                parsed: list[ActiveFault] = []
                for item in payload.get("faults", []):
                    parsed.append(
                        ActiveFault(
                            fault_type=str(item["type"]),
                            intensity=dict(item.get("intensity") or {}),
                            started_at=datetime.fromisoformat(item["started_at"]),
                            expires_at=datetime.fromisoformat(item["expires_at"]),
                        )
                    )
                self._faults = parsed
        except Exception:
            # Fail safe: retain cached faults until expires_at, emit ZERO logs or metrics.
            now = datetime.now(tz=UTC)
            self._faults = [f for f in self._faults if f.expires_at > now]

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)
            except TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None and self.controller_url:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
