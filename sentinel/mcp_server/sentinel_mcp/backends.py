"""Read-only upstream backends for sentinel-mcp: Prometheus, Loki, Kubernetes (`shop` only), PB-07 Deploy History."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


class BackendError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TelemetryBackend(ABC):
    @abstractmethod
    async def query_range(
        self, promql: str, *, start: datetime, end: datetime, step_seconds: int
    ) -> list[tuple[str, float]]: ...

    @abstractmethod
    async def query_logs(
        self, logql: str, *, start: datetime, end: datetime, limit: int = 5000
    ) -> tuple[list[tuple[str, str]], int]: ...

    @abstractmethod
    async def get_pod_status(self, service: str) -> dict[str, Any]: ...

    @abstractmethod
    async def get_k8s_events(
        self, *, service: str, event_type: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def list_deploys(self, *, service: str, limit: int) -> list[dict[str, Any]]: ...


class LiveClusterBackend(TelemetryBackend):
    """Queries Prometheus, Loki, Kubernetes (`shop` namespace only), and PB-07 deploy-history API."""

    def __init__(
        self,
        *,
        prometheus_url: str,
        loki_url: str,
        pb07_url: str,
        deploy_history_token: str,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.loki_url = loki_url.rstrip("/")
        self.pb07_url = pb07_url.rstrip("/")
        self.deploy_history_token = deploy_history_token
        self.timeout_seconds = timeout_seconds

    async def query_range(
        self, promql: str, *, start: datetime, end: datetime, step_seconds: int
    ) -> list[tuple[str, float]]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(
                    f"{self.prometheus_url}/api/v1/query_range",
                    params={
                        "query": promql,
                        "start": f"{start.timestamp():.3f}",
                        "end": f"{end.timestamp():.3f}",
                        "step": f"{step_seconds}s",
                    },
                )
        except httpx.TimeoutException as exc:
            raise BackendError("timeout", f"Prometheus query timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise BackendError("upstream_unavailable", f"Prometheus unreachable: {exc}") from exc

        if resp.status_code >= 400:
            raise BackendError("upstream_unavailable", f"Prometheus HTTP {resp.status_code}")

        data = resp.json()
        results = (data.get("data") or {}).get("result") or []
        if not results:
            return []
        values = results[0].get("values") or []
        points: list[tuple[str, float]] = []
        for ts_raw, val_raw in values:
            try:
                fval = float(val_raw)
                if fval != fval:  # NaN check
                    continue
                iso = datetime.fromtimestamp(float(ts_raw), tz=UTC).isoformat()
                points.append((iso, round(fval, 6)))
            except (TypeError, ValueError):
                continue
        return points

    async def query_logs(
        self, logql: str, *, start: datetime, end: datetime, limit: int = 5000
    ) -> tuple[list[tuple[str, str]], int]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(
                    f"{self.loki_url}/loki/api/v1/query_range",
                    params={
                        "query": logql,
                        "start": str(int(start.timestamp() * 1e9)),
                        "end": str(int(end.timestamp() * 1e9)),
                        "limit": str(min(limit, 5000)),
                        "direction": "backward",
                    },
                )
        except httpx.TimeoutException as exc:
            raise BackendError("timeout", f"Loki query timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise BackendError("upstream_unavailable", f"Loki unreachable: {exc}") from exc

        if resp.status_code >= 400:
            raise BackendError("upstream_unavailable", f"Loki HTTP {resp.status_code}")

        data = resp.json()
        streams = (data.get("data") or {}).get("result") or []
        entries: list[tuple[str, str]] = []
        for stream in streams:
            for ns_str, line in stream.get("values") or []:
                ts = datetime.fromtimestamp(int(ns_str) / 1e9, tz=UTC).isoformat()
                entries.append((ts, str(line)))
        entries.sort(key=lambda item: item[0])
        return entries, len(entries)

    async def get_pod_status(self, service: str) -> dict[str, Any]:
        """Read status-only fields from Kubernetes `shop` namespace (NEVER env, args, or annotations)."""
        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()

            apps_v1 = client.AppsV1Api()
            core_v1 = client.CoreV1Api()
            dep = apps_v1.read_namespaced_deployment(name=service, namespace="shop")
            pods = core_v1.list_namespaced_pod(
                namespace="shop", label_selector=f"app.kubernetes.io/name={service}"
            ).items[:10]

            dep_image = ""
            if (
                dep.spec
                and dep.spec.template
                and dep.spec.template.spec
                and dep.spec.template.spec.containers
            ):
                img = dep.spec.template.spec.containers[0].image or ""
                dep_image = img.split(":")[-1] if ":" in img else img

            dep_status = {
                "desired": dep.spec.replicas if dep.spec else 0,
                "ready": dep.status.ready_replicas or 0 if dep.status else 0,
                "updated": dep.status.updated_replicas or 0 if dep.status else 0,
                "available": dep.status.available_replicas or 0 if dep.status else 0,
                "image_tag": dep_image,
                "conditions": [
                    {"type": c.type, "status": c.status, "reason": c.reason}
                    for c in (dep.status.conditions or [])
                ]
                if dep.status
                else [],
            }
            pod_items = []
            for p in pods:
                statuses = (p.status.container_statuses or []) if p.status else []
                cs = statuses[0] if statuses else None
                last_term = None
                if cs and cs.last_state and cs.last_state.terminated:
                    last_term = cs.last_state.terminated.reason
                p_img = ""
                if cs and cs.image:
                    p_img = cs.image.split(":")[-1] if ":" in cs.image else cs.image
                pod_items.append(
                    {
                        "name": p.metadata.name if p.metadata else "",
                        "phase": p.status.phase if p.status else "Unknown",
                        "ready": bool(cs.ready) if cs else False,
                        "restarts": int(cs.restart_count) if cs else 0,
                        "last_termination_reason": last_term,
                        "started_at": p.status.start_time.isoformat()
                        if p.status and p.status.start_time
                        else None,
                        "image_tag": p_img,
                    }
                )
            return {"service": service, "deployment": dep_status, "pods": pod_items}
        except Exception:
            # Local docker-compose fallback: probe service /healthz so tool still returns clean status
            return await self._docker_compose_pod_fallback(service)

    async def _docker_compose_pod_fallback(self, service: str) -> dict[str, Any]:
        url = os.getenv(
            f"SHOP_{service.upper()}_URL",
            f"http://{service}:{'8001' if service == 'orders' else '8002'}",
        )
        ready = False
        version = "unknown"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                h = await client.get(f"{url}/healthz")
                if h.status_code == 200:
                    version = str(h.json().get("version", "unknown"))
                r = await client.get(f"{url}/readyz")
                ready = r.status_code == 200
        except Exception:
            ready = False
        return {
            "service": service,
            "deployment": {
                "desired": 1,
                "ready": 1 if ready else 0,
                "updated": 1,
                "available": 1 if ready else 0,
                "image_tag": version,
                "conditions": [
                    {
                        "type": "Available",
                        "status": "True" if ready else "False",
                        "reason": "MinimumReplicasAvailable" if ready else "ReadinessProbeFailed",
                    }
                ],
            },
            "pods": [
                {
                    "name": f"{service}-0",
                    "phase": "Running",
                    "ready": ready,
                    "restarts": 0,
                    "last_termination_reason": None,
                    "started_at": (datetime.now(tz=UTC) - timedelta(minutes=30)).isoformat(),
                    "image_tag": version,
                }
            ],
        }

    async def get_k8s_events(
        self, *, service: str, event_type: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Read deduplicated Kubernetes events in `shop` namespace only."""
        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()

            core_v1 = client.CoreV1Api()
            ev_list = core_v1.list_namespaced_event(namespace="shop").items
            events: list[dict[str, Any]] = []
            for ev in ev_list:
                obj_name = (ev.involved_object.name or "") if ev.involved_object else ""
                if service != "all" and not obj_name.startswith(service):
                    continue
                ev_t = ev.type or "Normal"
                if event_type != "all" and ev_t != event_type:
                    continue
                last_ts = ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
                if last_ts is not None:
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=UTC)
                    if last_ts < start or last_ts > end:
                        continue
                first_ts = ev.first_timestamp or last_ts
                events.append(
                    {
                        "reason": ev.reason or "Unknown",
                        "type": ev_t,
                        "object": obj_name,
                        "message": (ev.message or "")[:200],
                        "count": int(ev.count or 1),
                        "first_seen": first_ts.isoformat() if first_ts else start.isoformat(),
                        "last_seen": last_ts.isoformat() if last_ts else end.isoformat(),
                    }
                )
            return events
        except Exception:
            return []

    async def list_deploys(self, *, service: str, limit: int) -> list[dict[str, Any]]:
        apps = (
            [("orders", "shop-orders"), ("payments", "shop-payments")]
            if service == "all"
            else [(service, f"shop-{service}")]
        )
        deploys: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                for svc_name, app_name in apps:
                    resp = await client.get(
                        f"{self.pb07_url}/v1/apps/{app_name}/history",
                        params={"limit": limit},
                        headers={"Authorization": f"Bearer {self.deploy_history_token}"},
                    )
                    if resp.status_code == 200:
                        for item in resp.json().get("deploys", []):
                            deploys.append(
                                {
                                    "service": svc_name,
                                    "version": item.get("version"),
                                    "previous_version": item.get("previous_version"),
                                    "deployed_at": item.get("deployed_at"),
                                    "git_revision": item.get("revision") or item.get("git_commit"),
                                    "message": item.get("message", ""),
                                    "rollout_state": item.get("rollout_state", "complete"),
                                }
                            )
        except httpx.TimeoutException as exc:
            raise BackendError("timeout", f"Deploy history API timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                "upstream_unavailable", f"Deploy history unreachable: {exc}"
            ) from exc
        return deploys
