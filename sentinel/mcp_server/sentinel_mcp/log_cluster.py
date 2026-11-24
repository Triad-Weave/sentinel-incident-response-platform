"""Server-side log template clustering (ADR-006, Architecture p.24).

Reads up to 5,000 log lines, redacts secrets first, masks variable tokens (UUIDs, IPs,
numbers, quoted strings), groups by template, and keeps at most `max_groups` (<=20) with one
redacted example (<=300 chars) and `rate_per_minute` (<=15 points).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sentinel_common.redaction import redact_text

MAX_RAW_LINES = 5_000
MAX_EXAMPLE_CHARS = 300
MAX_RATE_POINTS = 15

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_ID_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_QUOTED_RE = re.compile(r'"[^"\n]{1,120}"|\'[^\'\n]{1,120}\'')
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?(?=(?:ms|s|us|ns|kb|mb|gb|%)?(?:\b|$))", re.IGNORECASE)


def extract_log_message(raw_line: str) -> str:
    """Pull a concise message + error details from a JSON log line (or raw text)."""
    try:
        parsed = json.loads(raw_line)
        if isinstance(parsed, dict):
            parts = [str(parsed.get("msg", ""))]
            if parsed.get("error.type"):
                parts.append(str(parsed["error.type"]))
            if parsed.get("error.message"):
                parts.append(str(parsed["error.message"]))
            if parsed.get("route"):
                parts.append(f"route={parsed['route']}")
            if parsed.get("status"):
                parts.append(f"status={parsed['status']}")
            return " | ".join(p for p in parts if p)
    except Exception:
        pass
    return raw_line.strip()


def mask_to_template(text: str) -> str:
    """Normalize variable values (UUIDs, IPs, hex IDs, numbers, long quoted values) into template tokens."""
    t = _UUID_RE.sub("<UUID>", text)
    t = _IP_RE.sub("<IP>", t)
    t = _HEX_ID_RE.sub("<ID>", t)

    # Keep short key error identifiers (e.g. KeyError: 'line_items') readable while masking dynamic values
    def _quote_sub(m: re.Match[str]) -> str:
        inner = m.group(0)
        if len(inner) <= 24 and " " not in inner:
            return inner
        return '"<STR>"'

    t = _QUOTED_RE.sub(_quote_sub, t)
    t = _NUM_RE.sub("<N>", t)
    return re.sub(r"\s+", " ", t).strip()[:220]


def cluster_log_lines(
    entries: list[tuple[str, str]],
    *,
    max_groups: int = 10,
    total_matches_override: int | None = None,
) -> dict[str, Any]:
    """Cluster (iso_timestamp, raw_line) entries into bounded templates with redacted examples."""
    bounded = entries[:MAX_RAW_LINES]
    truncated = len(entries) > MAX_RAW_LINES or (
        total_matches_override is not None and total_matches_override > len(bounded)
    )
    groups_map: dict[str, dict[str, Any]] = {}
    minute_counts: Counter[str] = Counter()

    for ts_iso, raw_line in bounded:
        # Redact BEFORE extracting examples (ADR-006)
        redacted_raw = redact_text(raw_line)
        msg = redact_text(extract_log_message(redacted_raw))
        tpl = mask_to_template(msg)

        bucket_min = (
            ts_iso[:16] + ":00Z"
            if len(ts_iso) >= 16
            else datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:00Z")
        )
        minute_counts[bucket_min] += 1

        if tpl not in groups_map:
            groups_map[tpl] = {
                "template": tpl,
                "count": 0,
                "first_seen": ts_iso,
                "last_seen": ts_iso,
                "example": msg[:MAX_EXAMPLE_CHARS],
            }
        g = groups_map[tpl]
        g["count"] += 1
        if ts_iso < g["first_seen"]:
            g["first_seen"] = ts_iso
        if ts_iso > g["last_seen"]:
            g["last_seen"] = ts_iso

    sorted_groups = sorted(groups_map.values(), key=lambda g: g["count"], reverse=True)
    clamped_groups = max(1, min(20, max_groups))
    if len(sorted_groups) > clamped_groups:
        truncated = True
    top_groups = sorted_groups[:clamped_groups]

    sorted_minutes = sorted(minute_counts.items())[-MAX_RATE_POINTS:]
    rate_per_minute = [[ts, cnt] for ts, cnt in sorted_minutes]

    total = total_matches_override if total_matches_override is not None else len(entries)
    return {
        "total_matches": total,
        "groups": top_groups,
        "rate_per_minute": rate_per_minute,
        "truncated": truncated,
    }
