"""Secret audit scanner (NFR-05) and ground-truth leak scanner (FR-CH-03)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from sentinel_common.redaction import (
    _AUTH_BEARER_RE,
    _AWS_KEY_RE,
    _BEARER_RE,
    _CARD_CANDIDATE_RE,
    _GITHUB_TOKEN_RE,
    _JSON_SECRET_RE,
    _JWT_RE,
    _KV_SECRET_RE,
    _PEM_RE,
    _URL_CRED_RE,
    luhn_valid,
)

STATIC_LEAK_TERMS = ("chaos", "inject", "ground_truth", "sentinel-chaos", "faultkit")


def _to_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, default=str)


def scan_for_secrets(
    payload: Any,
    *,
    canaries: Iterable[str] = (),
) -> list[str]:
    """Scan stored model requests or tool outputs for unredacted secrets or canary values."""
    text = _to_text(payload)
    hits: list[str] = []

    for canary in canaries:
        if canary and canary in text:
            hits.append("canary_secret_leaked")

    if _PEM_RE.search(text):
        hits.append("pem_key")
    if _JWT_RE.search(text):
        hits.append("jwt")
    if _AWS_KEY_RE.search(text):
        hits.append("aws_key")
    if _GITHUB_TOKEN_RE.search(text):
        hits.append("github_token")
    if _URL_CRED_RE.search(text):
        hits.append("url_credentials")

    for match in _AUTH_BEARER_RE.finditer(text):
        if not match.group(2).startswith("[REDACTED:"):
            hits.append("auth_bearer")
    for match in _BEARER_RE.finditer(text):
        if not match.group(2).startswith("[REDACTED:"):
            hits.append("bearer_token")
    for match in _JSON_SECRET_RE.finditer(text):
        if not match.group(2).startswith("[REDACTED:") and match.group(2) != "":
            hits.append("json_secret")
    for match in _KV_SECRET_RE.finditer(text):
        val = match.group(2)
        if not val.startswith("[REDACTED:") and val.lower() not in (
            "none",
            "null",
            "true",
            "false",
        ):
            hits.append("kv_secret")
    for match in _CARD_CANDIDATE_RE.finditer(text):
        if match.group("uuid") is not None:
            continue
        digits = "".join(c for c in (match.group("card") or "") if c.isdigit())
        if luhn_valid(digits):
            hits.append("card_number")

    return sorted(set(hits))


def scan_for_ground_truth_leaks(
    payload: Any,
    *,
    fault_ids: Iterable[str] = (),
    scenario_ids: Iterable[str] = (),
    controller_hostnames: Iterable[str] = ("chaos-controller",),
) -> list[str]:
    """Scan stored tool outputs and alert payloads for ground-truth leakage (FR-CH-03).

    Per Architecture.pdf p.16, this scans tool outputs and alert payloads ONLY — never the model's
    own free text, which legitimately mentions phrases like "fault type".
    """
    text = _to_text(payload)
    lower = text.lower()
    hits: list[str] = []

    for term in STATIC_LEAK_TERMS:
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lower):
            hits.append(f"term:{term}")

    for host in controller_hostnames:
        if host and host.lower() in lower:
            hits.append(f"hostname:{host}")

    for fid in fault_ids:
        if fid and fid in text:
            hits.append(f"fault_id:{fid}")

    for sid in scenario_ids:
        if sid and sid in text:
            hits.append(f"scenario_id:{sid}")

    return sorted(set(hits))
