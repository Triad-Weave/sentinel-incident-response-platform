"""Configuration for chaos-controller (namespace: sentinel-chaos)."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class ChaosSettings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///:memory:"
    operator_token: str = "dev-chaos-operator-token"
    orders_read_token: str = "dev-chaos-orders-token"
    payments_read_token: str = "dev-chaos-payments-token"
    log_level: str = "INFO"

    model_config = {"env_prefix": "CHAOS_"}
