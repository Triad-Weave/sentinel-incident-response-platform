"""SQLAlchemy 2 ORM models for the `sentinel` Postgres database (Architecture p.28-30)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class SentinelBase(DeclarativeBase):
    pass


JSONVariant = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Incident(SentinelBase):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    short_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    alert_service: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    suspected_service: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_incident_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id"), nullable=True
    )
    new_evidence_banner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    finding_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mitigated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Alert(SentinelBase):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("fingerprint", "starts_at", name="uq_alerts_fingerprint_starts_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    alertname: Mapped[str] = mapped_column(String(128), nullable=False)
    service: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="page")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="firing")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    annotations: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Investigation(SentinelBase):
    __tablename__ = "investigations"
    __table_args__ = (
        Index(
            "uq_one_running_investigation_per_incident",
            "incident_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class InvestigationStep(SentinelBase):
    __tablename__ = "investigation_steps"
    __table_args__ = (
        UniqueConstraint("investigation_id", "seq", name="uq_steps_investigation_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("investigations.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # triage_tool | tool | limit | error
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(String(200), nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    evidence_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class LlmCall(SentinelBase):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("investigations.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Finding(SentinelBase):
    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("investigations.id"), nullable=False, index=True
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True
    )
    body: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    suspected_service: Mapped[str] = mapped_column(String(32), nullable=False)
    fault_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action_service: Mapped[str | None] = mapped_column(String(32), nullable=True)
    limit_reached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    guardrail_notes: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ActionRequest(SentinelBase):
    __tablename__ = "action_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("findings.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # rollback | restart | none
    service: Mapped[str | None] = mapped_column(String(32), nullable=True)
    from_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="proposed", index=True
    )  # proposed | approved | rejected | executing | succeeded | failed | expired
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class IncidentEvent(SentinelBase):
    """Append-only incident timeline; Postgres insert trigger calls pg_notify('incident_events', ...)."""

    __tablename__ = "incident_events"
    __table_args__ = (UniqueConstraint("incident_id", "seq", name="uq_incident_events_seq"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class User(SentinelBase):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # viewer | responder
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Postmortem(SentinelBase):
    """Drafted Markdown postmortem (Should, FR-IC-05)."""

    __tablename__ = "postmortems"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id"), primary_key=True
    )
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class IncidentQuestion(SentinelBase):
    """Follow-up responder Q&A about an incident (Could, FR-AG-09)."""

    __tablename__ = "incident_questions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True
    )
    asked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
