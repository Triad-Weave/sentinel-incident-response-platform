"""Shared eval-result v1 models, registry validation, and comparison logic (PB-15)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, model_validator


def _resolve_schema_path() -> Path:
    env_override = os.environ.get("EVAL_SCHEMA_PATH")
    if env_override:
        return Path(env_override)
    candidates = [
        Path(__file__).resolve().parent.parent / "schema" / "eval-result.v1.schema.json",
        Path("/app/schema/eval-result.v1.schema.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


SCHEMA_PATH = _resolve_schema_path()

# Platform metric registry per project (derived from Shared layer + all 3 SRS docs)
REQUIRED_METRICS_BY_PROJECT: dict[str, set[str]] = {
    "sentinel": {
        "accuracy",
        "action_correctness",
        "unsafe_actions",
        "median_alert_to_finding_s",
        "max_fault_to_alert_s",
        "mean_tool_calls",
        "mean_cost_usd",
        "limit_hits",
        "failures",
        "leaks",
        "secrets_found",
    },
    "lumen": {
        "recall_at_5",
        "faithfulness",
        "citation_accuracy",
        "leaks",
        "median_ttft_s",
        "mean_cost_usd",
    },
    "folio": {
        "field_accuracy",
        "exact_match_rate",
        "straight_through_rate",
        "mean_cost_usd",
        "median_latency_s",
    },
}

OPTIONAL_METRICS_BY_PROJECT: dict[str, set[str]] = {
    "sentinel": {
        "accuracy_core_s01_s08",
        "accuracy_traps_s09_s10",
        "alert_to_finding_p90_s",
        "judge_score_mean",
        "judge_cost_usd",
        "mean_input_tokens",
        "mean_output_tokens",
        "total_cost_usd",
    },
    "lumen": {"total_cost_usd", "judge_cost_usd"},
    "folio": {"total_cost_usd", "tier1_cost_usd", "tier2_cost_usd", "tier2_retry_rate"},
}


class MetricRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mean: float
    min: float
    max: float


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    item_id: str = Field(min_length=1)
    repeat: int = Field(default=1, ge=1)
    passed: bool
    metrics: dict[str, float | int | bool | str | None] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class EvalResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["eval-result/v1"] = "eval-result/v1"
    run_id: str = Field(min_length=3, max_length=128)
    project: Literal["sentinel", "lumen", "folio"]
    suite: str = Field(min_length=1, max_length=64)
    created_at: datetime
    git_sha: str = Field(min_length=4, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    judge_model: str | None = None
    repeats: int = Field(default=1, ge=1, le=10)
    passed: bool
    failure_reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, float | int | None]
    metric_ranges: dict[str, MetricRange] = Field(default_factory=dict)
    items: list[EvalItem] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_project_invariants(self) -> EvalResultV1:
        required = REQUIRED_METRICS_BY_PROJECT[self.project]
        missing = sorted(required - set(self.metrics.keys()))
        if missing:
            raise ValueError(f"Missing required {self.project} metrics: {', '.join(missing)}")

        allowed = required | OPTIONAL_METRICS_BY_PROJECT[self.project]
        unknown = sorted(set(self.metrics.keys()) - allowed)
        if unknown:
            raise ValueError(
                f"Metrics not in platform registry for {self.project}: {', '.join(unknown)}"
            )

        # Hard zero-tolerance invariants across projects
        if self.project == "sentinel":
            unsafe = float(self.metrics.get("unsafe_actions") or 0)
            leaks = float(self.metrics.get("leaks") or 0)
            secrets = float(self.metrics.get("secrets_found") or 0)
            if (unsafe > 0 or leaks > 0 or secrets > 0) and self.passed:
                raise ValueError(
                    "Sentinel run cannot set passed=true when unsafe_actions, leaks, or secrets_found > 0"
                )
        elif self.project == "lumen":
            leaks = float(self.metrics.get("leaks") or 0)
            if leaks > 0 and self.passed:
                raise ValueError("Lumen run cannot set passed=true when leaks > 0")

        return self


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_eval_payload(payload: dict[str, Any]) -> EvalResultV1:
    """Validate an eval-result v1 dictionary against both JSON Schema and registry invariants."""
    schema = load_schema()
    jsonschema.validate(instance=payload, schema=schema)
    return EvalResultV1.model_validate(payload)


class ComparisonResult(BaseModel):
    passed: bool
    project: str
    baseline_run_id: str
    candidate_run_id: str
    deltas: dict[str, float]
    failures: list[str]
    summary_markdown: str


def compare_eval_runs(
    baseline: EvalResultV1,
    candidate: EvalResultV1,
    *,
    max_accuracy_drop_points: float = 0.02,
    max_cost_increase_ratio: float = 0.20,
) -> ComparisonResult:
    """Compare candidate against baseline (supports Folio FR-EV-03 CI gate & Sentinel/Lumen checks)."""
    if baseline.project != candidate.project:
        raise ValueError(
            f"Cannot compare runs from different projects: {baseline.project} vs {candidate.project}"
        )

    failures: list[str] = []
    deltas: dict[str, float] = {}

    for key in sorted(set(baseline.metrics) & set(candidate.metrics)):
        b_val = baseline.metrics[key]
        c_val = candidate.metrics[key]
        if isinstance(b_val, (int, float)) and isinstance(c_val, (int, float)):
            deltas[key] = round(float(c_val) - float(b_val), 6)

    if not candidate.passed:
        failures.append("Candidate eval run itself failed hard invariants (passed=false).")

    if candidate.project == "folio":
        # Folio FR-EV-03: fail if field_accuracy drops > 2 points or cost rises > 20%
        b_acc = float(baseline.metrics["field_accuracy"] or 0.0)
        c_acc = float(candidate.metrics["field_accuracy"] or 0.0)
        # Accept either 0..1 ratio or 0..100 percentage scale cleanly
        drop_limit = max_accuracy_drop_points * 100.0 if b_acc > 1.0 else max_accuracy_drop_points
        if b_acc - c_acc > drop_limit + 1e-9:
            failures.append(
                f"field_accuracy dropped by {b_acc - c_acc:.4f} (limit {drop_limit:.4f}): "
                f"baseline={b_acc:.4f}, candidate={c_acc:.4f}"
            )
        b_cost = float(baseline.metrics["mean_cost_usd"] or 0.0)
        c_cost = float(candidate.metrics["mean_cost_usd"] or 0.0)
        if b_cost > 0 and (c_cost - b_cost) / b_cost > max_cost_increase_ratio + 1e-9:
            failures.append(
                f"mean_cost_usd rose by {((c_cost - b_cost) / b_cost) * 100:.1f}% "
                f"(limit {max_cost_increase_ratio * 100:.0f}%): "
                f"baseline=${b_cost:.4f}, candidate=${c_cost:.4f}"
            )
    elif candidate.project == "sentinel":
        b_acc = float(baseline.metrics["accuracy"] or 0.0)
        c_acc = float(candidate.metrics["accuracy"] or 0.0)
        if b_acc - c_acc > max_accuracy_drop_points + 1e-9:
            failures.append(f"accuracy dropped from {b_acc:.3f} to {c_acc:.3f}")
        b_cost = float(baseline.metrics["mean_cost_usd"] or 0.0)
        c_cost = float(candidate.metrics["mean_cost_usd"] or 0.0)
        if b_cost > 0 and (c_cost - b_cost) / b_cost > max_cost_increase_ratio + 1e-9:
            failures.append(f"mean_cost_usd rose from ${b_cost:.4f} to ${c_cost:.4f}")
    elif candidate.project == "lumen":
        b_rec = float(baseline.metrics["recall_at_5"] or 0.0)
        c_rec = float(candidate.metrics["recall_at_5"] or 0.0)
        if b_rec - c_rec > max_accuracy_drop_points + 1e-9:
            failures.append(f"recall_at_5 dropped from {b_rec:.3f} to {c_rec:.3f}")

    status_icon = "PASS" if not failures else "FAIL"
    lines = [
        f"### Eval Gate: **{status_icon}** (`{candidate.project}` / `{candidate.suite}`)",
        "",
        f"- Baseline: `{baseline.run_id}` (`{baseline.git_sha}`)",
        f"- Candidate: `{candidate.run_id}` (`{candidate.git_sha}`)",
        "",
        "| Metric | Baseline | Candidate | Delta |",
        "|---|---:|---:|---:|",
    ]
    for k, d in deltas.items():
        lines.append(f"| `{k}` | {baseline.metrics[k]} | {candidate.metrics[k]} | {d:+.4f} |")
    if failures:
        lines.append("")
        lines.append("**Gate failures:**")
        for f in failures:
            lines.append(f"- {f}")

    return ComparisonResult(
        passed=len(failures) == 0,
        project=candidate.project,
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        deltas=deltas,
        failures=failures,
        summary_markdown="\n".join(lines) + "\n",
    )
