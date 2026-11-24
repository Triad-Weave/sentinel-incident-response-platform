"""Single secret redactor used in BOTH sentinel-mcp and sentinel-worker LLM client (NFR-05).

Strips before any text leaves MCP or enters an LLM call:
- PEM private keys
- JWTs
- AWS access keys
- GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_)
- URLs with embedded credentials (any scheme: http, https, postgresql, amqp, etc.)
- Authorization / Bearer header values
- password, passwd, secret, token, api_key, apikey, access_key in JSON, query strings, and key=value text
- Luhn-valid payment card numbers (13 to 19 digits), skipping full UUIDs first

Every match is replaced with `[REDACTED:<kind>]`.
"""

from __future__ import annotations

import re
from typing import Any

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)(?!\[REDACTED:)([^:@/\s]+):([^@/\s]+)@")
_AUTH_BEARER_RE = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*bearer\s+)(?!\[REDACTED:)([^\s\"',;&}]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)(?!\[REDACTED:)([A-Za-z0-9._~+/=-]{8,})")
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*)(?!bearer\b|\[REDACTED:)(?:(?:basic|digest|token|apikey)\s+)?([^\s\"',;&}]+)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:[a-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)[a-z0-9_.-]*)"\s*:\s*")((?:\\.|[^"\\])*)(")'
)
_KV_SECRET_RE = re.compile(
    r"(?i)(\b(?:[a-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[a-z0-9_.-]*)\s*[=:]\s*)([^\s\"',;&}]+)"
)
# Skip full UUIDs first so hex/hyphen neighbours (e.g. "card-4111...", "a4111...") still redact cards
_UUID_OR_CARD_RE = re.compile(
    r"(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"|(?P<card>(?<!\d)\d(?:[ -]?\d){12,18}(?!\d))"
)
# Backwards-compatible alias kept for callers/scanners
_CARD_CANDIDATE_RE = _UUID_OR_CARD_RE


def luhn_valid(number: str) -> bool:
    """Return True if a digit string (13..19 digits) passes the Luhn check."""
    digits = [int(ch) for ch in number if ch.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    checksum = 0
    parity = len(digits) % 2
    for idx, digit in enumerate(digits):
        if idx % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _replace_luhn_card(match: re.Match[str]) -> str:
    if match.group("uuid") is not None:
        return match.group(0)
    raw = match.group("card") or ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if luhn_valid(digits):
        return "[REDACTED:card_number]"
    return raw


def redact_text(text: str) -> str:
    """Redact all sensitive tokens, keys, credentials, and Luhn-valid card numbers from text."""
    if not text:
        return text
    out = _PEM_RE.sub("[REDACTED:pem_key]", text)
    out = _JWT_RE.sub("[REDACTED:jwt]", out)
    out = _AWS_KEY_RE.sub("[REDACTED:aws_key]", out)
    out = _GITHUB_TOKEN_RE.sub("[REDACTED:github_token]", out)
    out = _URL_CRED_RE.sub(r"\1[REDACTED:url_credentials]@", out)
    out = _AUTH_BEARER_RE.sub(r"\1[REDACTED:bearer]", out)
    out = _BEARER_RE.sub(r"\1[REDACTED:bearer]", out)
    out = _AUTH_HEADER_RE.sub(r"\1[REDACTED:authorization]", out)
    out = _JSON_SECRET_RE.sub(r"\1[REDACTED:secret]\3", out)

    def _kv_sub(m: re.Match[str]) -> str:
        val = m.group(2)
        if val.startswith("[REDACTED:"):
            return m.group(0)
        return f"{m.group(1)}[REDACTED:secret]"

    out = _KV_SECRET_RE.sub(_kv_sub, out)
    out = _UUID_OR_CARD_RE.sub(_replace_luhn_card, out)
    return out


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts, lists, and tuples without mutating input."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        result: dict[Any, Any] = {}
        for k, v in obj.items():
            key_lower = str(k).lower()
            if (
                isinstance(v, str)
                and any(
                    marker in key_lower
                    for marker in (
                        "password",
                        "passwd",
                        "secret",
                        "api_key",
                        "apikey",
                        "access_key",
                        "token",
                    )
                )
                and not v.startswith("[REDACTED:")
            ):
                result[k] = "[REDACTED:secret]"
            else:
                result[k] = redact_obj(v)
        return result
    if isinstance(obj, list):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(item) for item in obj)
    return obj
