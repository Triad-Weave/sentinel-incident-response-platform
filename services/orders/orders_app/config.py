"""Configuration for the Orders service (namespace: shop)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
DEFAULT_VERSION = (
    _VERSION_FILE.read_text(encoding="utf-8").strip() if _VERSION_FILE.exists() else "1.3.0"
)


class OrdersSettings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///:memory:"
    payments_url: str = "http://payments:8002"
    payments_timeout_seconds: float = 2.5
    pool_size: int = 5
    pool_timeout_seconds: float = 1.0
    chaos_controller_url: str = ""
    chaos_read_token: str = ""
    version: str = DEFAULT_VERSION
    log_level: str = "INFO"

    model_config = {"env_prefix": "ORDERS_"}
