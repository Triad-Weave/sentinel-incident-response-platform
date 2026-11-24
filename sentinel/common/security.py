"""Argon2 password hashing and signed httpOnly session cookies (FR-IC-01, ADR-011)."""

from __future__ import annotations

from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_HASHER = PasswordHasher()
SESSION_COOKIE_NAME = "sentinel_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 12


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def create_session_token(secret: str, *, user_id: str, email: str, role: str) -> str:
    serializer = URLSafeTimedSerializer(secret, salt="sentinel-session-v1")
    return serializer.dumps({"user_id": user_id, "email": email, "role": role})


def verify_session_token(secret: str, token: str) -> dict[str, Any] | None:
    serializer = URLSafeTimedSerializer(secret, salt="sentinel-session-v1")
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        if isinstance(data, dict) and {"user_id", "email", "role"} <= set(data.keys()):
            return data
        return None
    except (BadSignature, SignatureExpired):
        return None
