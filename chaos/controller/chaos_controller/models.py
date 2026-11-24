"""SQLAlchemy models for the isolated `chaos` database (active faults + ground-truth ledger)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ChaosBase(DeclarativeBase):
    pass


JSONVariant = JSON().with_variant(JSONB, "postgresql")


class FaultLedgerRow(ChaosBase):
    """Ground-truth ledger row — completely inaccessible from the `sentinel` namespace (FR-CH-03)."""

    __tablename__ = "ground_truth_ledger"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fault_type: Mapped[str] = mapped_column(String(64), nullable=False)
    service: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    intensity: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(tz=UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="operator")
    scenario_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    expected_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
