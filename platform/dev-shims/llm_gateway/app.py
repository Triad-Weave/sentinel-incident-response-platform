"""Local dev shim for the Shared Services LLM Gateway (PB-14, owned by Lumen teammate).

Implements the exact PB-14 contract:
- Per-project bearer API key & model allow-list
- Per-project daily USD budget enforcement (returns HTTP 429 with `daily LLM budget reached` once spent)
- Feature tag tracking (`X-LLM-Feature`: investigation | judge | postmortem | followup)
- Returns token usage + explicit `cost_usd` in response JSON & `X-LLM-Cost-USD` header
- Exports Prometheus metrics for the `LLM Gateway` Grafana dashboard
- Proxies to an upstream OpenAI-compatible provider if `UPSTREAM_LLM_API_KEY` is set, OR runs a
  realistic deterministic diagnostic mock mode for zero-cost offline local testing!
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

app = FastAPI(title="Cohort Platform LLM Gateway Dev Shim (PB-14)", version="1.0.0")
GW_REGISTRY = CollectorRegistry()

API_KEY_MAP = {
    os.getenv("LLM_GATEWAY_API_KEY", "dev-sentinel-gateway-key"): "sentinel",
    os.getenv("LUMEN_GATEWAY_API_KEY", "dev-lumen-gateway-key"): "lumen",
    os.getenv("FOLIO_GATEWAY_API_KEY", "dev-folio-gateway-key"): "folio",
}
DAILY_BUDGETS = {
    "sentinel": float(os.getenv("SENTINEL_DAILY_BUDGET_USD", "10.0")),
    "lumen": float(os.getenv("LUMEN_DAILY_BUDGET_USD", "5.0")),
    "folio": float(os.getenv("FOLIO_DAILY_BUDGET_USD", "5.0")),
}
ALLOWED_MODELS = {
    "gpt-4o-mini": (0.15 / 1_000_000, 0.60 / 1_000_000),
    "gpt-4o": (2.50 / 1_000_000, 10.00 / 1_000_000),
    "gemini-2.5-flash": (0.15 / 1_000_000, 0.60 / 1_000_000),
    "gemini-2.5-pro": (1.25 / 1_000_000, 10.00 / 1_000_000),
    "claude-3-5-sonnet": (3.00 / 1_000_000, 15.00 / 1_000_000),
}

_SPENT_BY_DAY_PROJECT: dict[tuple[str, str], float] = {}

COST_COUNTER = Counter(
    "llm_gateway_cost_usd_total",
    "Total LLM spend USD",
    labelnames=("project", "feature", "model"),
    registry=GW_REGISTRY,
)
TOKENS_COUNTER = Counter(
    "llm_gateway_tokens_total",
    "Total LLM tokens",
    labelnames=("project", "feature", "model", "kind"),
    registry=GW_REGISTRY,
)
LATENCY_HIST = Histogram(
    "llm_gateway_request_duration_seconds",
    "LLM gateway request latency seconds",
    labelnames=("project", "model"),
    registry=GW_REGISTRY,
)
BUDGET_REMAINING = Gauge(
    "llm_gateway_daily_budget_remaining_usd",
    "Remaining daily USD budget per project",
    labelnames=("project",),
    registry=GW_REGISTRY,
)


def _today() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


def _spent(project: str) -> float:
    return _SPENT_BY_DAY_PROJECT.get((_today(), project), 0.0)


def _record_spend(
    project: str, feature: str, model: str, cost: float, p_tok: int, c_tok: int
) -> None:
    key = (_today(), project)
    _SPENT_BY_DAY_PROJECT[key] = _SPENT_BY_DAY_PROJECT.get(key, 0.0) + cost
    COST_COUNTER.labels(project=project, feature=feature, model=model).inc(cost)
    TOKENS_COUNTER.labels(project=project, feature=feature, model=model, kind="prompt").inc(p_tok)
    TOKENS_COUNTER.labels(project=project, feature=feature, model=model, kind="completion").inc(
        c_tok
    )
    BUDGET_REMAINING.labels(project=project).set(
        max(0.0, DAILY_BUDGETS[project] - _SPENT_BY_DAY_PROJECT[key])
    )


def _mock_chat_completion(body: dict[str, Any]) -> dict[str, Any]:
    """Deterministic diagnostic response generator for offline local testing."""
    messages = body.get("messages", [])
    tools = body.get("tools") or []
    tool_choice = body.get("tool_choice")
    full_text = "\n".join(str(m.get("content") or "") for m in messages)
    tool_names = {t["function"]["name"] for t in tools if isinstance(t, dict) and "function" in t}

    # Judge / Postmortem / Follow-up plain text completions (no submit_finding tool)
    if "submit_finding" not in tool_names:
        if '"score"' in full_text or "judge_reference" in full_text:
            content = json.dumps(
                {
                    "score": 2,
                    "reason": "Hypothesis identifies the exact root-cause service, fault type, and mechanism supported by evidence.",
                }
            )
        elif "postmortem" in full_text.lower():
            content = (
                "# Incident Postmortem\n\n"
                "## Summary\nAutomated investigation isolated the service degradation and guided operator remediation.\n\n"
                "## Root Cause\nDiagnosed via golden signals, deploy history, and error log templates.\n\n"
                "## Follow-up Actions\n- Add regression test covering the fault path.\n- Monitor error rate and p95 latency.\n"
            )
        else:
            content = (
                "Based on the stored investigation timeline and evidence, the anomaly was isolated "
                "using golden signals, log templates, and recent deploy timestamps."
            )
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "gpt-4o-mini"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020},
        }

    # Agent tool-use loop: inspect triage evidence to make a realistic tool call or submit_finding
    has_log_call = any(
        m.get("role") == "tool" and "groups" in str(m.get("content", "")) for m in messages
    )
    force_submit = (
        isinstance(tool_choice, dict)
        and tool_choice.get("function", {}).get("name") == "submit_finding"
    )

    if not has_log_call and not force_submit and "search_logs" in tool_names:
        target_svc = (
            "payments"
            if '"payments"' in full_text and "payments" in full_text.lower()
            else "orders"
        )
        if "ServiceHighErrorRate" in full_text and "orders" in full_text.lower():
            target_svc = "orders"
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "gpt-4o-mini"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"Check error log templates on {target_svc} to confirm the failure signature.",
                        "tool_calls": [
                            {
                                "id": f"call_{uuid.uuid4().hex[:8]}",
                                "type": "function",
                                "function": {
                                    "name": "search_logs",
                                    "arguments": json.dumps(
                                        {
                                            "service": target_svc,
                                            "level": "error",
                                            "window_minutes": 15,
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 2200, "completion_tokens": 80, "total_tokens": 2280},
        }

    # Build finding grounded in evidence IDs present in context
    ev_ids = sorted(set(re.findall(r"ev-\d{2,3}", full_text))) or ["ev-01"]
    lower = full_text.lower()
    has_db_saturation = (
        "db_pool_acquire_timeout" in lower
        or "pool acquire timeout" in lower
        or "pool exhausted" in lower
        or bool(
            re.search(
                r'"db_pool_in_use_ratio"\s*:\s*\{[^}]*"current"\s*:\s*(?:0\.9\d*|1(?:\.0*)?)',
                lower,
            )
        )
    )
    if has_db_saturation:
        svc = (
            "payments"
            if "payments" in lower and "db_exhaustion" not in lower and "payments_pool" in lower
            else "orders"
        )
        fault_type = "db_exhaustion"
        action_type = "restart" if "restart" in json.dumps(tools) else "none"
        action_svc = svc if action_type == "restart" else None
        hypothesis = f"{svc.capitalize()} database connection pool reached saturation with pool acquire timeouts."
    elif "keyerror" in lower or "itemised" in lower or "1.4.0" in lower:
        svc, fault_type, action_type, action_svc = "orders", "bad_release", "rollback", "orders"
        hypothesis = "Orders 1.4.0 deployment introduced a KeyError on line_items right before errors climbed."
    elif "2.3.0" in lower or "fraud" in lower:
        svc, fault_type, action_type, action_svc = "payments", "bad_release", "rollback", "payments"
        hypothesis = "Payments 2.3.0 deployment caused latency degradation right after rollout."
    elif "latency" in lower or "p95" in lower:
        svc = "payments" if "payments" in lower else "orders"
        fault_type, action_type, action_svc = "latency", "none", None
        hypothesis = f"{svc.capitalize()} p95 latency spiked significantly with no correlating recent deploy."
    else:
        svc = "orders"
        fault_type, action_type, action_svc = "error_rate", "none", None
        hypothesis = (
            "Orders 5xx error ratio increased sharply without a correlating recent deployment."
        )

    finding_args = {
        "suspected_service": svc,
        "fault_type": fault_type,
        "hypothesis": hypothesis,
        "evidence": [
            {
                "evidence_id": eid,
                "claim": f"Observed anomalous signal supporting {fault_type} on {svc}.",
            }
            for eid in ev_ids[:3]
        ],
        "confidence": "high",
        "proposed_action": {
            "type": action_type,
            "service": action_svc,
            "rationale": f"Evidence points to {fault_type} on {svc}; recommended action is {action_type}.",
        },
    }
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "gpt-4o-mini"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Submitting evidence-backed diagnostic finding.",
                    "tool_calls": [
                        {
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": "submit_finding",
                                "arguments": json.dumps(finding_args),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 2800, "completion_tokens": 180, "total_tokens": 2980},
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/budget")
async def get_budget(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = (authorization or "").removeprefix("Bearer ").strip()
    project = API_KEY_MAP.get(token)
    if not project:
        raise HTTPException(status_code=401, detail="Invalid gateway API key")
    spent = _spent(project)
    limit = DAILY_BUDGETS[project]
    return {
        "project": project,
        "daily_budget_usd": limit,
        "spent_usd": round(spent, 6),
        "remaining_usd": round(max(0.0, limit - spent), 6),
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_llm_feature: str = Header(default="investigation"),
) -> Response:
    token = (authorization or "").removeprefix("Bearer ").strip()
    project = API_KEY_MAP.get(token)
    if not project:
        raise HTTPException(status_code=401, detail="Invalid gateway API key")

    body = await request.json()
    model = str(body.get("model", "gpt-4o-mini"))
    if model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400, detail=f"Model '{model}' is not on the platform allow-list"
        )

    if _spent(project) >= DAILY_BUDGETS[project]:
        raise HTTPException(status_code=429, detail="daily LLM budget reached")

    start = time.perf_counter()
    upstream_key = os.getenv("UPSTREAM_LLM_API_KEY", "").strip()
    upstream_base = os.getenv("UPSTREAM_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")

    if upstream_key:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{upstream_base}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {upstream_key}"},
            )
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
    else:
        data = _mock_chat_completion(body)

    LATENCY_HIST.labels(project=project, model=model).observe(time.perf_counter() - start)
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))

    in_rate, out_rate = ALLOWED_MODELS[model]
    billable_in = max(0, prompt_tokens - cached_tokens) + cached_tokens * 0.5
    cost_usd = round(billable_in * in_rate + completion_tokens * out_rate, 6)
    _record_spend(project, x_llm_feature, model, cost_usd, prompt_tokens, completion_tokens)

    data["gateway"] = {
        "project": project,
        "feature": x_llm_feature,
        "cost_usd": cost_usd,
        "cached_tokens": cached_tokens,
    }
    return Response(
        content=json.dumps(data),
        media_type="application/json",
        headers={"X-LLM-Cost-USD": f"{cost_usd:.6f}", "X-LLM-Feature": x_llm_feature},
    )


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(GW_REGISTRY), media_type=CONTENT_TYPE_LATEST)
