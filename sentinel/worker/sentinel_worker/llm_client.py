"""OpenAI-compatible LLM client pointed strictly at the PB-14 Platform LLM Gateway.

- Applies `sentinel_common.redaction.redact_obj` to EVERY outgoing payload (second redaction layer, NFR-05).
- 30-second client timeout; retries ONCE on connection error only so retries never stack with gateway (NFR-06).
- Fails immediately with `DailyBudgetReachedError` when the gateway rejects on daily budget exhaustion.
- Parses gateway-reported tokens, cached tokens, and cost_usd.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sentinel_common.redaction import redact_obj

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def compute_prompt_version() -> str:
    """Deterministic content hash of the prompts directory stamped on investigations & eval reports."""
    h = hashlib.sha256()
    for p in sorted(PROMPTS_DIR.glob("*.md")):
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


class LlmGatewayError(Exception):
    """Base error raised when an LLM gateway call fails."""


class DailyBudgetReachedError(LlmGatewayError):
    """Raised when the platform LLM gateway rejects a call because daily budget is spent."""


@dataclass
class LlmResponse:
    model: str
    content: str
    tool_calls: list[dict[str, Any]]
    raw_response: dict[str, Any]
    redacted_request: dict[str, Any]
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


class GatewayLlmClient:
    """HTTP client for the platform OpenAI-compatible LLM gateway (PB-14)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    async def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        feature: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        temperature: float = 0.0,
    ) -> LlmResponse:
        raw_payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            raw_payload["tools"] = tools
        if tool_choice is not None:
            raw_payload["tool_choice"] = tool_choice

        # Second redaction layer: strip secrets from EVERY outgoing message before sending or logging (NFR-05)
        redacted_payload = redact_obj(raw_payload)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-LLM-Feature": feature,
        }

        start = time.perf_counter()
        last_conn_err: Exception | None = None
        resp: httpx.Response | None = None

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self._transport
        ) as client:
            # Retry once ONLY on connection errors so retries never stack with gateway (NFR-06)
            for attempt in range(2):
                try:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=redacted_payload,
                        headers=headers,
                    )
                    break
                except httpx.ConnectError as exc:
                    last_conn_err = exc
                    if attempt == 0:
                        continue
                    raise LlmGatewayError(f"LLM gateway connection failed: {exc}") from exc
                except httpx.TimeoutException as exc:
                    raise LlmGatewayError(
                        f"LLM gateway timed out after {self.timeout_seconds}s"
                    ) from exc
                except httpx.HTTPError as exc:
                    raise LlmGatewayError(f"LLM gateway HTTP error: {exc}") from exc

        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp is None:
            raise LlmGatewayError(f"LLM gateway failed: {last_conn_err}")

        if resp.status_code == 429 or "daily llm budget reached" in resp.text.lower():
            raise DailyBudgetReachedError("daily LLM budget reached")
        if resp.status_code >= 400:
            raise LlmGatewayError(f"LLM gateway HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        content = str(msg.get("content") or "")
        tool_calls = list(msg.get("tool_calls") or [])

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        cached_tokens = int(
            (usage.get("prompt_tokens_details") or {}).get(
                "cached_tokens", (data.get("gateway") or {}).get("cached_tokens", 0)
            )
        )
        completion_tokens = int(usage.get("completion_tokens", 0))

        cost_usd = 0.0
        if "gateway" in data and data["gateway"].get("cost_usd") is not None:
            cost_usd = float(data["gateway"]["cost_usd"])
        elif resp.headers.get("X-LLM-Cost-USD"):
            cost_usd = float(resp.headers["X-LLM-Cost-USD"])

        return LlmResponse(
            model=str(data.get("model", model)),
            content=content,
            tool_calls=tool_calls,
            raw_response=data,
            redacted_request=redacted_payload,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
