"""Strict Finding JSON Schema, Incident States, Event Types, and API DTOs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IncidentStatus(StrEnum):
    INVESTIGATING = "Investigating"
    INVESTIGATION_FAILED = "Investigation failed"
    AWAITING_DECISION = "Awaiting decision"
    ACTION_IN_PROGRESS = "Action in progress"
    MONITORING = "Monitoring"
    MITIGATED = "Mitigated"
    REJECTED = "Rejected"
    RESOLVED = "Resolved"


OPEN_INCIDENT_STATUSES = frozenset(
    {
        IncidentStatus.INVESTIGATING.value,
        IncidentStatus.INVESTIGATION_FAILED.value,
        IncidentStatus.AWAITING_DECISION.value,
        IncidentStatus.ACTION_IN_PROGRESS.value,
        IncidentStatus.MONITORING.value,
        IncidentStatus.MITIGATED.value,
        IncidentStatus.REJECTED.value,
    }
)


class EventType(StrEnum):
    INCIDENT_OPENED = "incident.opened"
    ALERT_ATTACHED = "alert.attached"
    ALERT_RESOLVED = "alert.resolved"
    INVESTIGATION_STARTED = "investigation.started"
    STEP_RECORDED = "step.recorded"
    INVESTIGATION_LIMIT_REACHED = "investigation.limit_reached"
    INVESTIGATION_FAILED = "investigation.failed"
    FINDING_POSTED = "finding.posted"
    ACTION_PROPOSED = "action.proposed"
    ACTION_APPROVED = "action.approved"
    ACTION_REJECTED = "action.rejected"
    ACTION_EXECUTING = "action.executing"
    ACTION_SUCCEEDED = "action.succeeded"
    ACTION_FAILED = "action.failed"
    VERIFICATION_PASSED = "verification.passed"
    VERIFICATION_FAILED = "verification.failed"
    INCIDENT_STATUS_CHANGED = "incident.status_changed"
    POSTMORTEM_DRAFTED = "postmortem.drafted"


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^ev-[0-9]{2,3}$")
    claim: str = Field(min_length=1, max_length=200)


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["rollback", "restart", "none"]
    service: Literal["orders", "payments"] | None
    rationale: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_service_consistency(self) -> ProposedAction:
        if self.type == "none" and self.service is not None:
            self.service = None
        if self.type in ("rollback", "restart") and self.service is None:
            raise ValueError(f"proposed_action.service is required when type is '{self.type}'")
        return self


class AlternativeConsidered(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(min_length=1, max_length=200)
    why_rejected: str = Field(min_length=1, max_length=200)


class FindingSubmission(BaseModel):
    """Exact Finding schema submitted by the model via `submit_finding` (Architecture p.26-27)."""

    model_config = ConfigDict(extra="forbid")

    suspected_service: Literal["orders", "payments", "unknown"]
    fault_type: Literal[
        "bad_release",
        "latency",
        "error_rate",
        "db_exhaustion",
        "memory_leak",
        "cpu_saturation",
        "unknown",
    ]
    hypothesis: str = Field(min_length=20, max_length=600)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=8)
    confidence: Literal["low", "medium", "high"]
    proposed_action: ProposedAction
    alternatives_considered: list[AlternativeConsidered] = Field(default_factory=list, max_length=3)


def finding_tool_schema(*, enable_restart: bool = False) -> dict[str, Any]:
    """Return the exact OpenAI function-tool parameter JSON Schema for `submit_finding`."""
    action_enum = ["rollback", "restart", "none"] if enable_restart else ["rollback", "none"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "suspected_service",
            "fault_type",
            "hypothesis",
            "evidence",
            "confidence",
            "proposed_action",
        ],
        "properties": {
            "suspected_service": {"type": "string", "enum": ["orders", "payments", "unknown"]},
            "fault_type": {
                "type": "string",
                "enum": [
                    "bad_release",
                    "latency",
                    "error_rate",
                    "db_exhaustion",
                    "memory_leak",
                    "cpu_saturation",
                    "unknown",
                ],
            },
            "hypothesis": {"type": "string", "minLength": 20, "maxLength": 600},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["evidence_id", "claim"],
                    "properties": {
                        "evidence_id": {"type": "string", "pattern": "^ev-[0-9]{2,3}$"},
                        "claim": {"type": "string", "maxLength": 200},
                    },
                },
            },
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "proposed_action": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "service", "rationale"],
                "properties": {
                    "type": {"type": "string", "enum": action_enum},
                    "service": {"type": ["string", "null"], "enum": ["orders", "payments", None]},
                    "rationale": {"type": "string", "maxLength": 300},
                },
            },
            "alternatives_considered": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["hypothesis", "why_rejected"],
                    "properties": {
                        "hypothesis": {"type": "string", "maxLength": 200},
                        "why_rejected": {"type": "string", "maxLength": 200},
                    },
                },
            },
        },
    }


class ActionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=255)


class FollowupQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=1000)


class AlertmanagerAlert(BaseModel):
    status: Literal["firing", "resolved"] = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime
    endsAt: datetime | None = None
    fingerprint: str = Field(min_length=1)


class AlertmanagerWebhook(BaseModel):
    version: str = "4"
    status: str = "firing"
    alerts: list[AlertmanagerAlert] = Field(min_length=1)
