"""Resource-level fault actuators for db_exhaustion, memory_leak, and cpu_saturation (FR-CH-01, FR-CH-04).

Zero logging, zero metrics, zero trace spans.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from faultkit.client import FaultStateStore


class ResourceFaultManager:
    """Reconciles held DB connections, memory buffers, and CPU busy workers against active faults."""

    def __init__(
        self,
        *,
        store: FaultStateStore,
        acquire_db_conn: Callable[[], Awaitable[Any]] | None = None,
        release_db_conn: Callable[[Any], Awaitable[None]] | None = None,
        default_pool_size: int = 5,
        reconcile_interval_s: float = 1.0,
    ) -> None:
        self.store = store
        self._acquire_db_conn = acquire_db_conn
        self._release_db_conn = release_db_conn
        self._default_pool_size = default_pool_size
        self._reconcile_interval_s = reconcile_interval_s

        self._held_conns: list[Any] = []
        self._mem_buffers: list[bytearray] = []
        self._mem_last_alloc: float = 0.0
        self._cpu_stop = threading.Event()
        self._cpu_threads: list[threading.Thread] = []

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def held_connection_count(self) -> int:
        return len(self._held_conns)

    @property
    def allocated_memory_mb(self) -> float:
        return sum(len(b) for b in self._mem_buffers) / (1024 * 1024)

    async def reconcile_once(self) -> None:
        faults = self.store.active_faults()
        await self._reconcile_db_exhaustion(faults)
        self._reconcile_memory_leak(faults)
        self._reconcile_cpu_saturation(faults)

    async def _reconcile_db_exhaustion(self, faults: list[Any]) -> None:
        if self._acquire_db_conn is None or self._release_db_conn is None:
            return
        # Only hold connections in pods started BEFORE the fault injection (models a real leak;
        # restarting the pod starts a fresh pod after `started_at`, clearing the leak for FR-AG-08).
        db_faults = [
            f
            for f in faults
            if f.fault_type == "db_exhaustion" and self.store.pod_started_at <= f.started_at
        ]
        if not db_faults:
            await self._release_all_conns()
            return

        target = int(db_faults[0].intensity.get("connections") or self._default_pool_size)
        target = max(1, target)
        while len(self._held_conns) < target:
            try:
                conn = await asyncio.wait_for(self._acquire_db_conn(), timeout=1.0)
                self._held_conns.append(conn)
            except Exception:
                break
        while len(self._held_conns) > target:
            conn = self._held_conns.pop()
            try:
                await self._release_db_conn(conn)
            except Exception:
                pass

    async def _release_all_conns(self) -> None:
        while self._held_conns:
            conn = self._held_conns.pop()
            if self._release_db_conn is not None:
                try:
                    await self._release_db_conn(conn)
                except Exception:
                    pass

    def _reconcile_memory_leak(self, faults: list[Any]) -> None:
        mem_faults = [
            f
            for f in faults
            if f.fault_type == "memory_leak" and self.store.pod_started_at <= f.started_at
        ]
        if not mem_faults:
            self._mem_buffers.clear()
            self._mem_last_alloc = 0.0
            return

        mb_per_min = float(mem_faults[0].intensity.get("mb_per_minute", 10.0))
        max_mb = float(mem_faults[0].intensity.get("max_mb", 128.0))
        now = time.monotonic()
        if self._mem_last_alloc == 0.0:
            self._mem_last_alloc = now
        elapsed_min = max(0.0, (now - self._mem_last_alloc) / 60.0)
        to_alloc_mb = mb_per_min * elapsed_min
        if to_alloc_mb >= 1.0 and self.allocated_memory_mb < max_mb:
            chunk_mb = min(int(to_alloc_mb), int(max_mb - self.allocated_memory_mb))
            if chunk_mb > 0:
                self._mem_buffers.append(bytearray(chunk_mb * 1024 * 1024))
                self._mem_last_alloc = now

    def _reconcile_cpu_saturation(self, faults: list[Any]) -> None:
        cpu_faults = [f for f in faults if f.fault_type == "cpu_saturation"]
        if not cpu_faults:
            if self._cpu_threads:
                self._cpu_stop.set()
                for t in self._cpu_threads:
                    t.join(timeout=0.5)
                self._cpu_threads.clear()
            return

        desired_cores = max(1, min(4, int(cpu_faults[0].intensity.get("cores", 1))))
        if len(self._cpu_threads) < desired_cores:
            self._cpu_stop.clear()
            for _ in range(desired_cores - len(self._cpu_threads)):
                t = threading.Thread(target=self._cpu_spin, daemon=True)
                t.start()
                self._cpu_threads.append(t)

    def _cpu_spin(self) -> None:
        x = 1.0001
        while not self._cpu_stop.is_set():
            for _ in range(50_000):
                x = math.sqrt(x * x + 1.0)
            time.sleep(0.005)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.reconcile_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._reconcile_interval_s)
            except TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None:
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
        await self._release_all_conns()
        self._mem_buffers.clear()
        self._cpu_stop.set()
        for t in self._cpu_threads:
            t.join(timeout=0.5)
        self._cpu_threads.clear()
