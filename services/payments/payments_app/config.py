"""Configuration for the Payments service (namespace: shop)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
DEFAULT_VERSION = (
    _VERSION_FILE.read_text(encoding="utf-8").strip() if _VERSION_FILE.exists() else "2.2.0"
)


class PaymentsSettings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///:memory:"
    pool_size: int = 5
    pool_timeout_seconds: float = 1.0
    decline_rate: float = 0.0
    chaos_controller_url: str = ""
    chaos_read_token: str = ""
    canary_password: str = "canary-pw-sentinel-9f8e7d6c"
    canary_bearer_token: str = "canary-tok-sentinel-a1b2c3d4e5f6"
    canary_interval_seconds: int = 300
    version: str = DEFAULT_VERSION
    log_level: str = "INFO"

    model_config = {"env_prefix": "PAYMENTS_"}
