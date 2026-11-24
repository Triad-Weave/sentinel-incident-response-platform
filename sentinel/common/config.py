"""Shared configuration for Sentinel services."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class SentinelSettings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///:memory:"
    rabbitmq_url: str = "memory://"
    alertmanager_webhook_token: str = "dev-alertmanager-webhook-token"
    session_secret: str = "dev-session-secret-change-me"
    mcp_url: str = "http://sentinel-mcp:8003/mcp"
    mcp_token: str = "dev-mcp-token"

    # LLM Gateway (PB-14)
    llm_gateway_url: str = "http://llm-gateway:8090/v1"
    llm_gateway_api_key: str = "dev-sentinel-gateway-key"
    agent_model: str = "gpt-4o-mini"
    judge_model: str = "gpt-4o"

    # Hard limits (FR-AG-06)
    agent_max_tool_calls: int = 14
    agent_max_wall_seconds: int = 150
    agent_max_input_tokens: int = 90_000
    agent_max_output_tokens: int = 6_000
    agent_max_cost_usd: float = 0.35

    # Feature flags
    enable_restart_action: bool = True
    enable_postmortem: bool = True
    enable_followup_questions: bool = True

    # Rollback executor
    rollback_mode: str = "http_shim"  # crd | http_shim | git_pr
    pb07_controller_url: str = "http://rollback-controller:8095"
    deploy_history_read_token: str = "dev-deploy-history-token"

    # Canary audit values (NFR-05 — never leaked to LLM)
    canary_password: str = "canary-pw-sentinel-9f8e7d6c"
    canary_bearer_token: str = "canary-tok-sentinel-a1b2c3d4e5f6"

    log_level: str = "INFO"

    model_config = {"env_prefix": "SENTINEL_", "extra": "ignore"}
