"""Orders SQLAlchemy model and instrumented connection pool (FR-TS-01, FR-OB-01)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from platform_telemetry.metrics import COMMON_LABELS, base_labels
from prometheus_client import Gauge, Histogram
from sqlalchemy import JSON, DateTime, Numeric, String, Uuid
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DB_POOL_IN_USE = Gauge(
    "db_pool_connections_in_use",
    "Number of database connections currently checked out",
    labelnames=COMMON_LABELS,
)
DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "Configured database connection pool capacity",
    labelnames=COMMON_LABELS,
)
DB_POOL_WAIT = Histogram(
    "db_pool_wait_seconds",
    "Time spent waiting for a database connection from the pool",
    labelnames=COMMON_LABELS,
    buckets=(0.001, 0.005, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


class OrdersBase(DeclarativeBase):
    pass


class OrderRow(OrdersBase):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    total_cents: Mapped[int] = mapped_column(Numeric(12, 0), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    payment_auth_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(tz=UTC)
    )


class PoolAcquireTimeoutError(TimeoutError):
    """Raised when DB pool capacity is exhausted beyond pool_timeout_seconds."""


class InstrumentedPool:
    """Wraps AsyncEngine with bounded semaphore accounting so pool saturation works on both Postgres and SQLite."""

    def __init__(
        self,
        *,
        database_url: str,
        pool_size: int,
        pool_timeout_s: float,
        service: str = "orders",
        project: str = "sentinel",
        engine: AsyncEngine | None = None,
    ) -> None:
        self.pool_size = max(1, pool_size)
        self.pool_timeout_s = pool_timeout_s
        self.labels = base_labels(project=project, service=service)
        self._sem = asyncio.Semaphore(self.pool_size)
        self._in_use = 0
        self._owns_engine = engine is None
        if engine is not None:
            self.engine = engine
        elif database_url.startswith("sqlite"):
            self.engine = create_async_engine(database_url)
        else:
            self.engine = create_async_engine(
                database_url,
                pool_size=self.pool_size,
                max_overflow=0,
                pool_timeout=self.pool_timeout_s,
                pool_pre_ping=True,
            )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        DB_POOL_SIZE.labels(**self.labels).set(self.pool_size)
        DB_POOL_IN_USE.labels(**self.labels).set(0)

    async def init_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(OrdersBase.metadata.create_all)

    async def acquire_raw(self) -> AsyncConnection:
        """Checked out by faultkit during db_exhaustion to genuinely saturate the pool."""
        start = time.perf_counter()
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.pool_timeout_s)
        except TimeoutError as exc:
            DB_POOL_WAIT.labels(**self.labels).observe(time.perf_counter() - start)
            raise PoolAcquireTimeoutError(
                f"connection pool acquire timed out after {self.pool_timeout_s:.2f}s (in_use={self._in_use}/{self.pool_size})"
            ) from exc
        DB_POOL_WAIT.labels(**self.labels).observe(time.perf_counter() - start)
        self._in_use += 1
        DB_POOL_IN_USE.labels(**self.labels).set(self._in_use)
        try:
            return await self.engine.connect()
        except Exception:
            self._in_use -= 1
            DB_POOL_IN_USE.labels(**self.labels).set(self._in_use)
            self._sem.release()
            raise

    async def release_raw(self, conn: AsyncConnection) -> None:
        try:
            await conn.close()
        finally:
            self._in_use = max(0, self._in_use - 1)
            DB_POOL_IN_USE.labels(**self.labels).set(self._in_use)
            self._sem.release()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        start = time.perf_counter()
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.pool_timeout_s)
        except TimeoutError as exc:
            DB_POOL_WAIT.labels(**self.labels).observe(time.perf_counter() - start)
            raise PoolAcquireTimeoutError(
                f"database connection pool acquire timeout after {self.pool_timeout_s:.2f}s ({self._in_use}/{self.pool_size} in use)"
            ) from exc
        DB_POOL_WAIT.labels(**self.labels).observe(time.perf_counter() - start)
        self._in_use += 1
        DB_POOL_IN_USE.labels(**self.labels).set(self._in_use)
        try:
            async with self.session_factory() as sess:
                yield sess
        finally:
            self._in_use = max(0, self._in_use - 1)
            DB_POOL_IN_USE.labels(**self.labels).set(self._in_use)
            self._sem.release()

    async def dispose(self) -> None:
        if self._owns_engine:
            await self.engine.dispose()
