"""MCP tool client supporting official Streamable HTTP MCP transport, HTTP fallback, and local registry."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

import httpx


class McpToolClient(ABC):
    @abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class HttpMcpToolClient(McpToolClient):
    """Calls sentinel-mcp over Streamable HTTP MCP or authenticated `/v1/tools/{name}`."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 15.0,
        use_mcp_sdk: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.use_mcp_sdk = use_mcp_sdk

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Try official MCP Streamable HTTP client when pointing at /mcp
        if self.use_mcp_sdk and self.base_url.endswith("/mcp"):
            try:
                import httpx2
                from mcp import ClientSession
                from mcp.client.streamable_http import streamable_http_client

                auth_headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
                async with (
                    httpx2.AsyncClient(
                        headers=auth_headers, timeout=self.timeout_seconds
                    ) as mcp_http_client,
                    streamable_http_client(self.base_url, http_client=mcp_http_client) as (
                        read_stream,
                        write_stream,
                    ),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    await session.initialize()
                    res = await session.call_tool(name, arguments)
                    if res.content:
                        text = getattr(res.content[0], "text", None)
                        if text:
                            return json.loads(text)
            except Exception:
                # Fall back cleanly to /v1/tools/{name} on the same server host
                pass

        root_url = self.base_url[:-4] if self.base_url.endswith("/mcp") else self.base_url
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(
                    f"{root_url}/v1/tools/{name}",
                    json=arguments,
                    headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
                )
            if resp.status_code >= 400:
                return {
                    "error": {
                        "code": "upstream_unavailable",
                        "message": f"MCP server HTTP {resp.status_code}",
                    }
                }
            return resp.json()
        except httpx.TimeoutException:
            return {"error": {"code": "timeout", "message": f"MCP tool '{name}' timed out"}}
        except Exception as exc:
            return {"error": {"code": "upstream_unavailable", "message": str(exc)}}


class InProcessMcpToolClient(McpToolClient):
    """Wraps `SentinelToolRegistry` directly in-process for deterministic tests."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.registry.call_tool(name, arguments)
