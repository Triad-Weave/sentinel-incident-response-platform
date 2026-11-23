"""Hard execution budget checks for Sentinel investigations (FR-AG-06, BUILD_PROMPT section 9.1)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetLimits:
    max_tool_calls: int = 14
    max_wall_seconds: float = 150.0
    max_input_tokens: int = 90_000
    max_output_tokens: int = 6_000
    max_cost_usd: float = 0.35


def check_budget(
    limits: BudgetLimits,
    *,
    elapsed_seconds: float,
    tool_calls: int,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    for_tool: bool,
) -> str | None:
    """Return a human-readable limit breach reason if any hard budget is met/exceeded, else None."""
    if elapsed_seconds >= limits.max_wall_seconds:
        return f"wall time limit reached ({elapsed_seconds:.1f}s >= {limits.max_wall_seconds}s)"
    if for_tool and tool_calls >= limits.max_tool_calls:
        return f"tool call limit reached ({tool_calls} >= {limits.max_tool_calls})"
    if not for_tool:
        if input_tokens >= limits.max_input_tokens:
            return f"input token limit reached ({input_tokens} >= {limits.max_input_tokens})"
        if output_tokens >= limits.max_output_tokens:
            return f"output token limit reached ({output_tokens} >= {limits.max_output_tokens})"
        if cost_usd >= limits.max_cost_usd:
            return f"cost limit reached (${cost_usd:.4f} >= ${limits.max_cost_usd:.2f})"
    return None
