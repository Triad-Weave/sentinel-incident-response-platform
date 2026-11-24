"""Deterministic post-model guardrails applied before persisting any finding (FR-AG-05, BUILD_PROMPT 9.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sentinel_common.schemas import EvidenceItem, FindingSubmission


@dataclass(frozen=True)
class GuardrailOutcome:
    evidence: list[EvidenceItem]
    confidence: str
    action_type: str
    action_service: str | None
    notes: list[str]


def has_recent_deploy(
    deploys: list[dict[str, Any]],
    service: str,
    alert_started_at: datetime,
) -> bool:
    """Return True iff `service` was deployed within 15 minutes before `alert_started_at`."""
    ref = (
        alert_started_at.replace(tzinfo=UTC)
        if alert_started_at.tzinfo is None
        else alert_started_at.astimezone(UTC)
    )
    window_start = ref - timedelta(minutes=15)
    window_end = ref + timedelta(minutes=5)
    for d in deploys:
        if d.get("service") != service:
            continue
        dep_raw = d.get("deployed_at")
        if not dep_raw:
            continue
        dep_dt = datetime.fromisoformat(str(dep_raw).replace("Z", "+00:00"))
        if dep_dt.tzinfo is None:
            dep_dt = dep_dt.replace(tzinfo=UTC)
        else:
            dep_dt = dep_dt.astimezone(UTC)
        if window_start <= dep_dt <= window_end:
            return True
    return False


def apply_guardrails(
    finding: FindingSubmission,
    *,
    valid_evidence_ids: set[str],
    recent_deploys: list[dict[str, Any]],
    alert_started_at: datetime,
    limit_reached: bool,
    enable_restart: bool,
) -> GuardrailOutcome:
    """Apply all deterministic safety guardrails from BUILD_PROMPT section 9.1 step 9."""
    notes: list[str] = []
    filtered_evidence = [e for e in finding.evidence if e.evidence_id in valid_evidence_ids]
    dropped = len(finding.evidence) - len(filtered_evidence)
    final_confidence: str = finding.confidence

    if dropped > 0:
        notes.append(
            f"Dropped {dropped} unknown evidence id(s) not produced by this investigation."
        )
    if not filtered_evidence:
        final_confidence = "low"
        notes.append("No valid evidence ids remained; confidence downgraded to low.")

    if limit_reached and final_confidence == "high":
        final_confidence = "medium"
        notes.append("Confidence capped at medium because an investigation limit was reached.")

    final_action_type: str = finding.proposed_action.type
    final_action_service: str | None = finding.proposed_action.service

    if final_action_type == "restart" and not enable_restart:
        final_action_type = "none"
        final_action_service = None
        notes.append("Restart action disabled by configuration; downgraded to none.")

    if final_action_type != "none" and final_action_service != finding.suspected_service:
        notes.append(
            f"Downgraded {final_action_type} on {final_action_service} to none: "
            f"action service does not match suspected_service ({finding.suspected_service})."
        )
        final_action_type = "none"
        final_action_service = None

    if final_action_type == "rollback" and final_action_service is not None:
        if not has_recent_deploy(recent_deploys, final_action_service, alert_started_at):
            notes.append(
                f"Downgraded rollback on {final_action_service} to none: no deploy found within 15 minutes before alert."
            )
            final_action_type = "none"
            final_action_service = None

    return GuardrailOutcome(
        evidence=filtered_evidence,
        confidence=final_confidence,
        action_type=final_action_type,
        action_service=final_action_service,
        notes=notes,
    )
