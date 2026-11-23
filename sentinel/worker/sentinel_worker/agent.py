"""Core Sentinel investigation agent loop (ADR-002, FR-AG-01..07, NFR-01..06).

Flow:
1. Deterministic triage pack (3 MCP calls, no LLM):
   - get_golden_signals(window_minutes=30)
   - list_recent_deploys(service="all", lookback_minutes=120, limit=5)
   - get_pod_status(service=alert_service)
2. Single tool-use loop with hard limits (14 calls, 150s, 90k in, 6k out, $0.35),
   mid-investigation attached-alert awareness, untrusted-data wrapping, and 3-consecutive-tool-error cap.
3. Strict `submit_finding` JSON schema + evidence_id existence check + 1 repair attempt.
4. Deterministic guardrails (rollback needs deploy within 15m before alert; confidence capped on limit_reached;
   `from_version` read strictly from `get_pod_status`, never from model output).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sentinel_common.db import append_incident_event, notify_local_listeners
from sentinel_common.models import (
    ActionRequest,
    Alert,
    Finding,
    Incident,
    Investigation,
    InvestigationStep,
    LlmCall,
)
from sentinel_common.redaction import redact_obj
from sentinel_common.schemas import (
    EventType,
    FindingSubmission,
    IncidentStatus,
    finding_tool_schema,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel_worker.budget import BudgetLimits, check_budget
from sentinel_worker.guardrails import apply_guardrails, has_recent_deploy
from sentinel_worker.llm_client import (
    DailyBudgetReachedError,
    GatewayLlmClient,
    LlmGatewayError,
    compute_prompt_version,
    load_prompt,
)
from sentinel_worker.mcp_client import McpToolClient

MCP_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_golden_signals",
            "description": "Get golden signals (request_rate, error_ratio, latency_p95, db_pool_in_use_ratio) with baseline, current, change_pct, and onset_at for orders and payments.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "window_minutes": {
                        "type": "integer",
                        "minimum": 10,
                        "maximum": 60,
                        "default": 30,
                    },
                    "end_time": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_metrics",
            "description": "Query one allow-listed metric series for orders or payments in namespace shop.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["service", "metric"],
                "properties": {
                    "service": {"type": "string", "enum": ["orders", "payments"]},
                    "metric": {
                        "type": "string",
                        "enum": [
                            "request_rate",
                            "error_ratio",
                            "latency_p50",
                            "latency_p95",
                            "latency_p99",
                            "orders_per_minute",
                            "payment_failure_rate",
                            "upstream_error_ratio",
                            "upstream_latency_p95",
                            "db_pool_in_use_ratio",
                            "db_pool_wait_p95",
                            "pod_restarts",
                            "cpu_usage",
                            "memory_usage",
                        ],
                    },
                    "window_minutes": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 60,
                        "default": 30,
                    },
                    "step_seconds": {
                        "type": "integer",
                        "minimum": 15,
                        "maximum": 300,
                        "default": 60,
                    },
                    "end_time": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_logs",
            "description": "Search and cluster service logs by template (redacted, bounded).",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["service"],
                "properties": {
                    "service": {"type": "string", "enum": ["orders", "payments"]},
                    "level": {
                        "type": "string",
                        "enum": ["error", "warn", "info", "any"],
                        "default": "error",
                    },
                    "contains": {"type": "string", "maxLength": 100},
                    "window_minutes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60,
                        "default": 15,
                    },
                    "max_groups": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                    "end_time": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pod_status",
            "description": "Inspect Deployment and Pod status fields (never env vars/args/annotations).",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["service"],
                "properties": {"service": {"type": "string", "enum": ["orders", "payments"]}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_k8s_events",
            "description": "Inspect deduplicated Kubernetes events in namespace shop.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "service": {
                        "type": "string",
                        "enum": ["orders", "payments", "all"],
                        "default": "all",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["Warning", "Normal", "all"],
                        "default": "all",
                    },
                    "window_minutes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60,
                        "default": 30,
                    },
                    "end_time": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_deploys",
            "description": "List recent deploys from the PB-07 deploy-history API.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "service": {
                        "type": "string",
                        "enum": ["orders", "payments", "all"],
                        "default": "all",
                    },
                    "lookback_minutes": {
                        "type": "integer",
                        "minimum": 10,
                        "maximum": 360,
                        "default": 120,
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    "end_time": {"type": "string"},
                },
            },
        },
    },
]


# Alias preserved for backwards compatibility with existing callers/tests
AgentLimits = BudgetLimits


def _wrap_untrusted(evidence_id: str, tool_name: str, result: dict[str, Any]) -> str:
    payload = {"evidence_id": evidence_id, "tool": tool_name, "result": result}
    return f'<untrusted_tool_output evidence_id="{evidence_id}">\n{json.dumps(payload, default=str)}\n</untrusted_tool_output>'


def _truncate_reasoning(text: str | None) -> str:
    if not text:
        return ""
    words = text.strip().split()
    return " ".join(words[:25])


def _build_tools_schema(*, enable_restart: bool) -> list[dict[str, Any]]:
    submit_def = {
        "type": "function",
        "function": {
            "name": "submit_finding",
            "description": "Submit the final structured diagnostic finding and proposed action.",
            "parameters": finding_tool_schema(enable_restart=enable_restart),
        },
    }
    return [*MCP_TOOL_DEFINITIONS, submit_def]


class InvestigationAgent:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        mcp_client: McpToolClient,
        llm_client: GatewayLlmClient,
        model: str = "gpt-4o-mini",
        limits: AgentLimits | None = None,
        enable_restart: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self.mcp_client = mcp_client
        self.llm_client = llm_client
        self.model = model
        self.limits = limits or AgentLimits()
        self.enable_restart = enable_restart
        self.prompt_version = compute_prompt_version()

    async def run_investigation(self, incident_id: uuid.UUID) -> Investigation:
        start_mono = time.perf_counter()
        async with self.session_factory() as session:
            incident = await session.get(Incident, incident_id)
            if incident is None:
                raise ValueError(f"Incident {incident_id} not found")

            # Enforce single running investigation per incident
            existing_running = (
                await session.execute(
                    select(Investigation).where(
                        Investigation.incident_id == incident_id,
                        Investigation.status == "running",
                    )
                )
            ).scalar_one_or_none()
            if existing_running is not None:
                return existing_running

            prev_attempts = (
                await session.execute(
                    select(func.coalesce(func.max(Investigation.attempt), 0)).where(
                        Investigation.incident_id == incident_id
                    )
                )
            ).scalar_one()

            inv = Investigation(
                incident_id=incident_id,
                attempt=int(prev_attempts) + 1,
                status="running",
                model=self.model,
                prompt_version=self.prompt_version,
                started_at=datetime.now(tz=UTC),
                heartbeat_at=datetime.now(tz=UTC),
            )
            session.add(inv)
            incident.status = IncidentStatus.INVESTIGATING.value
            incident.new_evidence_banner = False
            await session.flush()

            ev = await append_incident_event(
                session,
                incident_id=incident_id,
                event_type=EventType.INVESTIGATION_STARTED.value,
                actor="sentinel-worker",
                payload={
                    "investigation_id": str(inv.id),
                    "attempt": inv.attempt,
                    "model": self.model,
                },
            )
            alerts = list(
                (
                    await session.execute(
                        select(Alert)
                        .where(Alert.incident_id == incident_id)
                        .order_by(Alert.received_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()
            await notify_local_listeners(incident_id, ev.seq)

        seen_alert_ids = {a.id for a in alerts}
        alert_service = (
            incident.alert_service if incident.alert_service in ("orders", "payments") else "orders"
        )
        alert_started_at = incident.started_at
        if alert_started_at.tzinfo is None:
            alert_started_at = alert_started_at.replace(tzinfo=UTC)

        step_seq = 0
        llm_seq = 0
        evidence_ids: set[str] = set()
        recent_deploys_cache: list[dict[str, Any]] = []
        pod_tags_cache: dict[str, str] = {}
        consecutive_tool_errors = 0
        limit_reached = False
        limit_reason: str | None = None

        async def _record_step(
            *,
            kind: str,
            tool_name: str | None,
            tool_input: dict[str, Any],
            summary: str,
            output: dict[str, Any],
            evidence_id: str | None,
            reasoning: str | None,
            latency_ms: int,
        ) -> None:
            nonlocal step_seq
            step_seq += 1
            safe_out = redact_obj(output)
            async with self.session_factory() as sess:
                step = InvestigationStep(
                    investigation_id=inv.id,
                    seq=step_seq,
                    kind=kind,
                    tool_name=tool_name,
                    input=tool_input,
                    summary=summary[:160],
                    output=safe_out,
                    evidence_id=evidence_id,
                    reasoning=reasoning,
                    latency_ms=latency_ms,
                )
                sess.add(step)
                db_inv = await sess.get(Investigation, inv.id)
                if db_inv is not None:
                    db_inv.heartbeat_at = datetime.now(tz=UTC)
                    if kind in ("triage_tool", "tool"):
                        db_inv.tool_calls = inv.tool_calls
                ev_row = await append_incident_event(
                    sess,
                    incident_id=incident_id,
                    event_type=EventType.STEP_RECORDED.value,
                    actor="sentinel-worker",
                    payload={
                        "investigation_id": str(inv.id),
                        "seq": step_seq,
                        "kind": kind,
                        "tool_name": tool_name,
                        "summary": summary[:160],
                        "evidence_id": evidence_id,
                        "reasoning": reasoning,
                    },
                )
                await sess.commit()
            await notify_local_listeners(incident_id, ev_row.seq)

        async def _exec_mcp_tool(
            tool_name: str,
            tool_args: dict[str, Any],
            *,
            kind: str,
            reasoning: str | None = None,
        ) -> tuple[str, dict[str, Any]]:
            inv.tool_calls += 1
            ev_id = f"ev-{inv.tool_calls:02d}"
            t0 = time.perf_counter()
            res = await self.mcp_client.call_tool(tool_name, tool_args)
            lat_ms = int((time.perf_counter() - t0) * 1000)

            is_err = isinstance(res, dict) and "error" in res
            if is_err:
                summary = f"error from {tool_name}: {res['error'].get('message', '')}"[:160]
                await _record_step(
                    kind="error",
                    tool_name=tool_name,
                    tool_input=tool_args,
                    summary=summary,
                    output=res,
                    evidence_id=None,
                    reasoning=reasoning,
                    latency_ms=lat_ms,
                )
                return "", res

            evidence_ids.add(ev_id)
            if tool_name == "list_recent_deploys" and isinstance(res.get("deploys"), list):
                recent_deploys_cache.clear()
                recent_deploys_cache.extend(res["deploys"])
            elif tool_name == "get_pod_status":
                svc_name = str(res.get("service") or tool_args.get("service") or "")
                tag = str((res.get("deployment") or {}).get("image_tag") or "")
                if svc_name and tag:
                    pod_tags_cache[svc_name] = tag

            summary = str(res.get("summary") or f"{tool_name} completed")[:160]
            await _record_step(
                kind=kind,
                tool_name=tool_name,
                tool_input=tool_args,
                summary=summary,
                output=res,
                evidence_id=ev_id,
                reasoning=reasoning,
                latency_ms=lat_ms,
            )
            return ev_id, res

        def _check_limits(*, for_tool: bool) -> str | None:
            return check_budget(
                self.limits,
                elapsed_seconds=time.perf_counter() - start_mono,
                tool_calls=inv.tool_calls,
                input_tokens=inv.input_tokens,
                output_tokens=inv.output_tokens,
                cost_usd=inv.cost_usd,
                for_tool=for_tool,
            )

        async def _call_model(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            tool_choice: Any = "auto",
        ):
            nonlocal llm_seq
            llm_seq += 1
            try:
                resp = await self.llm_client.chat_completion(
                    model=self.model,
                    messages=messages,
                    feature="investigation",
                    tools=tools,
                    tool_choice=tool_choice,
                    temperature=0.0,
                )
            except LlmGatewayError as exc:
                async with self.session_factory() as sess:
                    sess.add(
                        LlmCall(
                            investigation_id=inv.id,
                            seq=llm_seq,
                            model=self.model,
                            request=redact_obj({"model": self.model, "messages": messages}),
                            response=None,
                            status="failed",
                            error=str(exc),
                        )
                    )
                    await sess.commit()
                raise

            inv.input_tokens += resp.prompt_tokens
            inv.cached_tokens += resp.cached_tokens
            inv.output_tokens += resp.completion_tokens
            inv.cost_usd = round(inv.cost_usd + resp.cost_usd, 6)

            async with self.session_factory() as sess:
                sess.add(
                    LlmCall(
                        investigation_id=inv.id,
                        seq=llm_seq,
                        model=resp.model,
                        request=resp.redacted_request,
                        response=resp.raw_response,
                        prompt_tokens=resp.prompt_tokens,
                        cached_tokens=resp.cached_tokens,
                        completion_tokens=resp.completion_tokens,
                        cost_usd=resp.cost_usd,
                        latency_ms=resp.latency_ms,
                        status="ok",
                    )
                )
                db_inv = await sess.get(Investigation, inv.id)
                if db_inv is not None:
                    db_inv.heartbeat_at = datetime.now(tz=UTC)
                    db_inv.input_tokens = inv.input_tokens
                    db_inv.cached_tokens = inv.cached_tokens
                    db_inv.output_tokens = inv.output_tokens
                    db_inv.cost_usd = inv.cost_usd
                await sess.commit()
            return resp

        try:
            # 1. Deterministic Triage Pack (3 calls, no model turn — Architecture p.21)
            triage_blocks: list[str] = []
            triage_calls: list[tuple[str, dict[str, Any], str]] = [
                (
                    "get_golden_signals",
                    {"window_minutes": 30},
                    "Initial golden signals across orders and payments",
                ),
                (
                    "list_recent_deploys",
                    {"service": "all", "lookback_minutes": 120, "limit": 5},
                    "Check recent deployments before alert onset",
                ),
                (
                    "get_pod_status",
                    {"service": alert_service},
                    f"Check pod and deployment status for {alert_service}",
                ),
            ]
            for t_name, t_args, t_reason in triage_calls:
                breach = _check_limits(for_tool=True)
                if breach:
                    limit_reached = True
                    limit_reason = breach
                    break
                ev_id, res = await _exec_mcp_tool(
                    t_name, t_args, kind="triage_tool", reasoning=t_reason
                )
                triage_blocks.append(_wrap_untrusted(ev_id or "error", t_name, res))

            # Build stable prompt prefix (system prompt + tools never edited mid-run for prompt caching)
            system_prompt = load_prompt("system.md")
            tools_schema = _build_tools_schema(enable_restart=self.enable_restart)
            alert_summary = {
                "incident_short_id": incident.short_id,
                "title": incident.title,
                "alert_service": alert_service,
                "started_at": alert_started_at.isoformat(),
                "alerts": [
                    {
                        "alertname": a.alertname,
                        "service": a.service,
                        "severity": a.severity,
                        "status": a.status,
                        "starts_at": a.starts_at.isoformat(),
                        "summary": (a.annotations or {}).get("summary", ""),
                    }
                    for a in alerts
                ],
            }
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Alert summary:\n{json.dumps(alert_summary, default=str)}\n\n"
                        f"Deterministic triage pack:\n" + "\n\n".join(triage_blocks)
                    ),
                },
            ]

            submitted_finding: FindingSubmission | None = None
            repair_used = False

            # 2. Main single tool-use loop
            while submitted_finding is None and not limit_reached:
                # Inject any newly attached alerts that arrived mid-investigation (Arch p.32)
                async with self.session_factory() as sess:
                    current_alerts = list(
                        (await sess.execute(select(Alert).where(Alert.incident_id == incident_id)))
                        .scalars()
                        .all()
                    )
                new_alerts = [a for a in current_alerts if a.id not in seen_alert_ids]
                if new_alerts:
                    for na in new_alerts:
                        seen_alert_ids.add(na.id)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Additional alert(s) attached to this open incident: "
                                + json.dumps(
                                    [
                                        {
                                            "alertname": a.alertname,
                                            "service": a.service,
                                            "starts_at": a.starts_at.isoformat(),
                                        }
                                        for a in new_alerts
                                    ]
                                )
                            ),
                        }
                    )

                breach = _check_limits(for_tool=False)
                if breach:
                    limit_reached = True
                    limit_reason = breach
                    break

                resp = await _call_model(messages, tools_schema, tool_choice="auto")
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.content}
                if resp.tool_calls:
                    assistant_msg["tool_calls"] = resp.tool_calls
                messages.append(assistant_msg)

                if not resp.tool_calls:
                    # Ask model to call a tool or submit_finding
                    messages.append(
                        {
                            "role": "user",
                            "content": "Please call an observation tool or call submit_finding with your conclusion.",
                        }
                    )
                    continue

                reasoning = _truncate_reasoning(resp.content)
                for tc in resp.tool_calls:
                    tc_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")
                    fn_obj = tc.get("function") or {}
                    fn_name = str(fn_obj.get("name") or "")
                    raw_args = fn_obj.get("arguments") or "{}"
                    try:
                        parsed_args = (
                            json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        )
                    except json.JSONDecodeError as exc:
                        parsed_args = None
                        parse_err = str(exc)
                    else:
                        parse_err = ""

                    if fn_name == "submit_finding":
                        valid, val_err = self._validate_finding(
                            parsed_args, evidence_ids, parse_err
                        )
                        if valid is not None:
                            submitted_finding = valid
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc_id,
                                    "content": json.dumps({"accepted": True}),
                                }
                            )
                            break

                        # Invalid finding gets exactly ONE repair attempt (Architecture p.21)
                        if not repair_used:
                            repair_used = True
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc_id,
                                    "content": json.dumps(
                                        {
                                            "error": "invalid_finding",
                                            "details": val_err,
                                            "valid_evidence_ids": sorted(evidence_ids),
                                        }
                                    ),
                                }
                            )
                            break
                        else:
                            limit_reached = True
                            limit_reason = f"finding validation failed after repair: {val_err}"
                            break

                    # Normal read-only MCP tool call
                    tool_breach = _check_limits(for_tool=True)
                    if tool_breach:
                        limit_reached = True
                        limit_reason = tool_breach
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": json.dumps({"error": "budget_reached"}),
                            }
                        )
                        break

                    if parsed_args is None:
                        ev_id, tool_res = "", {"error": {"code": "bad_input", "message": parse_err}}
                    else:
                        ev_id, tool_res = await _exec_mcp_tool(
                            fn_name, parsed_args, kind="tool", reasoning=reasoning
                        )

                    if isinstance(tool_res, dict) and "error" in tool_res:
                        consecutive_tool_errors += 1
                    else:
                        consecutive_tool_errors = 0

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": _wrap_untrusted(ev_id or "error", fn_name, tool_res),
                        }
                    )

                    # Three tool errors in a row end the run with a partial finding (Arch p.23)
                    if consecutive_tool_errors >= 3:
                        limit_reached = True
                        limit_reason = "three consecutive tool errors"
                        break

            # 3. Handle limit breach -> record limit event + one forced submit_finding call (FR-AG-06)
            if submitted_finding is None and limit_reached:
                await _record_step(
                    kind="limit",
                    tool_name=None,
                    tool_input={},
                    summary=f"Limit reached: {limit_reason}"[:160],
                    output={"reason": limit_reason},
                    evidence_id=None,
                    reasoning=None,
                    latency_ms=0,
                )
                async with self.session_factory() as sess:
                    ev_lim = await append_incident_event(
                        sess,
                        incident_id=incident_id,
                        event_type=EventType.INVESTIGATION_LIMIT_REACHED.value,
                        actor="sentinel-worker",
                        payload={"investigation_id": str(inv.id), "reason": limit_reason},
                    )
                    await sess.commit()
                await notify_local_listeners(incident_id, ev_lim.seq)

                # Attempt one forced call if time remains (< max_wall_seconds + 15s grace)
                if (time.perf_counter() - start_mono) < (self.limits.max_wall_seconds + 10.0):
                    try:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"Budget reached ({limit_reason}): report what you have found so far "
                                    f"by calling submit_finding immediately. Valid evidence_ids: {sorted(evidence_ids)}"
                                ),
                            }
                        )
                        forced_resp = await _call_model(
                            messages,
                            tools_schema,
                            tool_choice={
                                "type": "function",
                                "function": {"name": "submit_finding"},
                            },
                        )
                        for tc in forced_resp.tool_calls:
                            fn_obj = tc.get("function") or {}
                            if fn_obj.get("name") == "submit_finding":
                                raw_a = fn_obj.get("arguments") or "{}"
                                parsed_a = (
                                    json.loads(raw_a) if isinstance(raw_a, str) else dict(raw_a)
                                )
                                valid, _ = self._validate_finding(parsed_a, evidence_ids, "")
                                if valid is not None:
                                    submitted_finding = valid
                                    break
                    except LlmGatewayError:
                        # Fall back to deterministic partial finding rather than losing progress
                        pass

            # 4. Deterministic fallback finding if still none
            if submitted_finding is None:
                fallback_ev = sorted(evidence_ids)[0] if evidence_ids else "ev-01"
                submitted_finding = FindingSubmission.model_validate(
                    {
                        "suspected_service": alert_service,
                        "fault_type": "unknown",
                        "hypothesis": (
                            f"Investigation hit a hard limit ({limit_reason or 'budget'}) before "
                            f"confirming root cause on {alert_service}; manual inspection required."
                        ),
                        "evidence": [
                            {
                                "evidence_id": fallback_ev,
                                "claim": "Partial evidence collected prior to limit.",
                            }
                        ],
                        "confidence": "low",
                        "proposed_action": {
                            "type": "none",
                            "service": None,
                            "rationale": "Partial investigation only; no automated remediation proposed.",
                        },
                    }
                )

            # 5. Deterministic Guardrails (BUILD_PROMPT section 9.1 step 9)
            guard = apply_guardrails(
                submitted_finding,
                valid_evidence_ids=evidence_ids,
                recent_deploys=recent_deploys_cache,
                alert_started_at=alert_started_at,
                limit_reached=limit_reached,
                enable_restart=self.enable_restart,
            )
            guardrail_notes = guard.notes
            final_action_type = guard.action_type
            final_action_service = guard.action_service
            final_confidence = guard.confidence

            # Model NEVER supplies `from_version`; fetch live tag from `get_pod_status`
            from_version: str | None = None
            if final_action_type in ("rollback", "restart") and final_action_service is not None:
                from_version = pod_tags_cache.get(final_action_service)
                if not from_version:
                    _, pod_res = await _exec_mcp_tool(
                        "get_pod_status",
                        {"service": final_action_service},
                        kind="tool",
                        reasoning=f"Confirm live image tag on {final_action_service} before proposing action.",
                    )
                    from_version = str((pod_res.get("deployment") or {}).get("image_tag") or "")

            finding_body = submitted_finding.model_dump()
            finding_body["evidence"] = [e.model_dump() for e in guard.evidence]
            finding_body["confidence"] = final_confidence
            finding_body["proposed_action"]["type"] = final_action_type
            finding_body["proposed_action"]["service"] = final_action_service
            finding_body["investigation_id"] = str(inv.id)
            finding_body["limit_reached"] = limit_reached
            finding_body["guardrail_notes"] = guardrail_notes
            finding_body["created_at"] = datetime.now(tz=UTC).isoformat()

            # 6. Persist Finding, ActionRequest, and transition Incident -> Awaiting decision
            now = datetime.now(tz=UTC)
            async with self.session_factory() as sess:
                db_inv = await sess.get(Investigation, inv.id)
                db_inc = await sess.get(Incident, incident_id)
                assert db_inv is not None and db_inc is not None

                db_inv.status = "limit_reached" if limit_reached else "completed"
                db_inv.ended_at = now
                db_inv.heartbeat_at = now
                db_inv.tool_calls = inv.tool_calls
                db_inv.input_tokens = inv.input_tokens
                db_inv.cached_tokens = inv.cached_tokens
                db_inv.output_tokens = inv.output_tokens
                db_inv.cost_usd = inv.cost_usd

                finding_row = Finding(
                    investigation_id=db_inv.id,
                    incident_id=incident_id,
                    body=finding_body,
                    suspected_service=submitted_finding.suspected_service,
                    fault_type=submitted_finding.fault_type,
                    confidence=final_confidence,
                    action_type=final_action_type,
                    action_service=final_action_service,
                    limit_reached=limit_reached,
                    guardrail_notes=guardrail_notes,
                )
                sess.add(finding_row)
                await sess.flush()

                action_row = ActionRequest(
                    incident_id=incident_id,
                    finding_id=finding_row.id,
                    action=final_action_type,
                    service=final_action_service,
                    from_version=from_version,
                    status="proposed",
                    expires_at=now + timedelta(minutes=15),
                )
                sess.add(action_row)
                await sess.flush()

                db_inc.status = IncidentStatus.AWAITING_DECISION.value
                db_inc.suspected_service = submitted_finding.suspected_service
                db_inc.finding_at = now

                ev1 = await append_incident_event(
                    sess,
                    incident_id=incident_id,
                    event_type=EventType.FINDING_POSTED.value,
                    actor="sentinel-worker",
                    payload={
                        "finding_id": str(finding_row.id),
                        "suspected_service": finding_row.suspected_service,
                        "fault_type": finding_row.fault_type,
                        "confidence": finding_row.confidence,
                        "limit_reached": limit_reached,
                    },
                )
                ev2 = await append_incident_event(
                    sess,
                    incident_id=incident_id,
                    event_type=EventType.ACTION_PROPOSED.value,
                    actor="sentinel-worker",
                    payload={
                        "action_request_id": str(action_row.id),
                        "action": action_row.action,
                        "service": action_row.service,
                        "from_version": action_row.from_version,
                    },
                )
                ev3 = await append_incident_event(
                    sess,
                    incident_id=incident_id,
                    event_type=EventType.INCIDENT_STATUS_CHANGED.value,
                    actor="sentinel-worker",
                    payload={"status": IncidentStatus.AWAITING_DECISION.value},
                )
                await sess.commit()

            for e in (ev1, ev2, ev3):
                await notify_local_listeners(incident_id, e.seq)
            return db_inv

        except (DailyBudgetReachedError, LlmGatewayError, Exception) as exc:
            # NFR-06: never crash or leave incident stuck; transition to Investigation failed with Retry
            reason = str(exc)[:500]
            now = datetime.now(tz=UTC)
            async with self.session_factory() as sess:
                db_inv = await sess.get(Investigation, inv.id)
                db_inc = await sess.get(Incident, incident_id)
                if db_inv is not None:
                    db_inv.status = "failed"
                    db_inv.failure_reason = reason
                    db_inv.ended_at = now
                    db_inv.heartbeat_at = now
                if db_inc is not None:
                    db_inc.status = IncidentStatus.INVESTIGATION_FAILED.value
                ev_f1 = await append_incident_event(
                    sess,
                    incident_id=incident_id,
                    event_type=EventType.INVESTIGATION_FAILED.value,
                    actor="sentinel-worker",
                    payload={"investigation_id": str(inv.id), "reason": reason},
                )
                ev_f2 = await append_incident_event(
                    sess,
                    incident_id=incident_id,
                    event_type=EventType.INCIDENT_STATUS_CHANGED.value,
                    actor="sentinel-worker",
                    payload={"status": IncidentStatus.INVESTIGATION_FAILED.value, "reason": reason},
                )
                await sess.commit()
            await notify_local_listeners(incident_id, ev_f1.seq)
            await notify_local_listeners(incident_id, ev_f2.seq)
            assert db_inv is not None
            return db_inv

    def _validate_finding(
        self,
        parsed_args: dict[str, Any] | None,
        valid_evidence_ids: set[str],
        parse_err: str,
    ) -> tuple[FindingSubmission | None, str]:
        if parsed_args is None:
            return None, f"Invalid JSON arguments: {parse_err}"
        try:
            finding = FindingSubmission.model_validate(parsed_args)
        except ValidationError as exc:
            return None, str(exc)

        missing_ev = [
            e.evidence_id for e in finding.evidence if e.evidence_id not in valid_evidence_ids
        ]
        if missing_ev:
            return None, f"Evidence IDs not produced by this investigation: {', '.join(missing_ev)}"
        return finding, ""

    def _has_recent_deploy(
        self,
        deploys: list[dict[str, Any]],
        service: str,
        alert_started_at: datetime,
    ) -> bool:
        """Delegate to `sentinel_worker.guardrails.has_recent_deploy`."""
        return has_recent_deploy(deploys, service, alert_started_at)
