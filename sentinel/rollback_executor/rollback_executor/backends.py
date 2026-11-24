"""Rollback backends: PB-07 Kubernetes RollbackRequest CRD (primary), HTTP dev shim, and ADR-003 Git PR fallback."""

from __future__ import annotations

import asyncio
import base64
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, cast

import httpx
import yaml

# Fixed, unmodifiable allow-list mapping (Architecture p.19)
SERVICE_TO_ARGO_APP: dict[str, str] = {
    "orders": "shop-orders",
    "payments": "shop-payments",
}


@dataclass
class RollbackOutcome:
    succeeded: bool
    from_revision: str | None = None
    to_revision: str | None = None
    to_version: str | None = None
    git_commit: str | None = None
    pr_url: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "fromRevision": self.from_revision,
            "toRevision": self.to_revision,
            "to_version": self.to_version,
            "gitCommit": self.git_commit,
            "pr_url": self.pr_url,
            "message": self.message,
        }


class RollbackBackend(ABC):
    @abstractmethod
    async def execute(
        self,
        *,
        request_name: str,
        service: str,
        expected_current_revision: str,
        reason: str,
        timeout_seconds: float = 300.0,
    ) -> RollbackOutcome: ...


class CrdRollbackBackend(RollbackBackend):
    """Primary PB-07 backend: creates & watches `platform.cohort.dev/v1 RollbackRequest` in `sentinel`.

    Holds ZERO Git credentials. Least privilege enforced by RBAC + ValidatingAdmissionPolicy (NFR-04).
    """

    GROUP = "platform.cohort.dev"
    VERSION = "v1"
    PLURAL = "rollbackrequests"
    NAMESPACE = "sentinel"

    async def execute(
        self,
        *,
        request_name: str,
        service: str,
        expected_current_revision: str,
        reason: str,
        timeout_seconds: float = 300.0,
    ) -> RollbackOutcome:
        if service not in SERVICE_TO_ARGO_APP:
            return RollbackOutcome(
                succeeded=False, message=f"Disallowed rollback service: {service}"
            )
        if not request_name.startswith("inc-"):
            return RollbackOutcome(
                succeeded=False, message="RollbackRequest name must start with 'inc-'"
            )

        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        api = client.CustomObjectsApi()
        body = {
            "apiVersion": f"{self.GROUP}/{self.VERSION}",
            "kind": "RollbackRequest",
            "metadata": {"name": request_name, "namespace": self.NAMESPACE},
            "spec": {
                "app": SERVICE_TO_ARGO_APP[service],
                "expectedCurrentRevision": expected_current_revision,
                "reason": reason[:200],
            },
        }
        try:
            api.create_namespaced_custom_object(
                group=self.GROUP,
                version=self.VERSION,
                namespace=self.NAMESPACE,
                plural=self.PLURAL,
                body=body,
            )
        except ApiException as exc:
            if exc.status != 409:  # 409 AlreadyExists is idempotent
                return RollbackOutcome(
                    succeeded=False,
                    from_revision=expected_current_revision,
                    message=f"RollbackRequest creation denied ({exc.status}): {exc.reason}",
                )

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() <= deadline:
            obj = cast(
                dict[str, Any],
                api.get_namespaced_custom_object(
                    group=self.GROUP,
                    version=self.VERSION,
                    namespace=self.NAMESPACE,
                    plural=self.PLURAL,
                    name=request_name,
                ),
            )
            status = obj.get("status") or {}
            phase = str(status.get("phase") or "Pending")
            if phase == "Succeeded":
                return RollbackOutcome(
                    succeeded=True,
                    from_revision=status.get("fromRevision"),
                    to_revision=status.get("toRevision"),
                    git_commit=status.get("gitCommit"),
                    message=str(status.get("message") or "Rollback succeeded"),
                )
            if phase == "Failed":
                return RollbackOutcome(
                    succeeded=False,
                    from_revision=status.get("fromRevision") or expected_current_revision,
                    message=str(status.get("message") or "Rollback failed"),
                )
            await asyncio.sleep(3.0)

        return RollbackOutcome(
            succeeded=False,
            from_revision=expected_current_revision,
            message=f"Timed out waiting {int(timeout_seconds)}s for RollbackRequest {request_name}",
        )


class HttpShimRollbackBackend(RollbackBackend):
    """Local dev adapter talking to `platform/dev-shims/rollback_controller`."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    async def execute(
        self,
        *,
        request_name: str,
        service: str,
        expected_current_revision: str,
        reason: str,
        timeout_seconds: float = 300.0,
    ) -> RollbackOutcome:
        if service not in SERVICE_TO_ARGO_APP:
            return RollbackOutcome(
                succeeded=False, message=f"Disallowed rollback service: {service}"
            )
        if not request_name.startswith("inc-"):
            return RollbackOutcome(
                succeeded=False, message="RollbackRequest name must start with 'inc-'"
            )

        payload = {
            "name": request_name,
            "namespace": "sentinel",
            "app": SERVICE_TO_ARGO_APP[service],
            "expectedCurrentRevision": expected_current_revision,
            "reason": reason[:200],
        }
        try:
            async with httpx.AsyncClient(
                timeout=min(30.0, timeout_seconds), transport=self._transport
            ) as client:
                resp = await client.post(f"{self.base_url}/v1/rollbacks", json=payload)
        except Exception as exc:
            return RollbackOutcome(
                succeeded=False,
                from_revision=expected_current_revision,
                message=f"Platform rollback controller unreachable: {exc}",
            )

        if resp.status_code >= 400:
            return RollbackOutcome(
                succeeded=False,
                from_revision=expected_current_revision,
                message=f"Rollback rejected ({resp.status_code}): {resp.text[:200]}",
            )

        data = resp.json()
        return RollbackOutcome(
            succeeded=data.get("phase") == "Succeeded",
            from_revision=data.get("fromRevision"),
            to_revision=data.get("toRevision"),
            to_version=data.get("toVersion"),
            git_commit=data.get("gitCommit"),
            message=str(data.get("message") or ""),
        )


class GuardedGitPrRollbackBackend(RollbackBackend):
    """ADR-003 fallback if PB-07 platform controller lands late: opens a 1-field revert PR on release.yaml."""

    ALLOWED_FILES = {
        "orders": "apps/shop/orders/release.yaml",
        "payments": "apps/shop/payments/release.yaml",
    }

    def __init__(
        self,
        *,
        repo: str | None = None,
        token: str | None = None,
        base_branch: str = "main",
        api_base: str = "https://api.github.com",
    ) -> None:
        self.repo = repo or os.getenv("GITOPS_GITHUB_REPO", "")
        self.token = token or os.getenv("GITOPS_GITHUB_TOKEN", "")
        self.base_branch = base_branch
        self.api_base = api_base.rstrip("/")

    async def execute(
        self,
        *,
        request_name: str,
        service: str,
        expected_current_revision: str,
        reason: str,
        timeout_seconds: float = 300.0,
    ) -> RollbackOutcome:
        if service not in self.ALLOWED_FILES:
            return RollbackOutcome(succeeded=False, message=f"Disallowed service: {service}")
        if not self.repo or not self.token:
            return RollbackOutcome(
                succeeded=False,
                from_revision=expected_current_revision,
                message="ADR-003 fallback GitHub credentials not configured",
            )

        file_path = self.ALLOWED_FILES[service]
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            # Fetch last 2 commits touching ONLY this release.yaml file
            commits_resp = await client.get(
                f"{self.api_base}/repos/{self.repo}/commits",
                params={"path": file_path, "sha": self.base_branch, "per_page": 2},
            )
            if commits_resp.status_code != 200 or len(commits_resp.json()) < 2:
                return RollbackOutcome(
                    succeeded=False,
                    from_revision=expected_current_revision,
                    message="Unable to resolve previous release.yaml commit for guarded revert",
                )
            commits = commits_resp.json()
            prev_sha = commits[1]["sha"]

            # Fetch current and previous contents
            curr_file = (
                await client.get(
                    f"{self.api_base}/repos/{self.repo}/contents/{file_path}",
                    params={"ref": self.base_branch},
                )
            ).json()
            prev_file = (
                await client.get(
                    f"{self.api_base}/repos/{self.repo}/contents/{file_path}",
                    params={"ref": prev_sha},
                )
            ).json()

            curr_yaml = yaml.safe_load(base64.b64decode(curr_file["content"]).decode("utf-8"))
            prev_yaml = yaml.safe_load(base64.b64decode(prev_file["content"]).decode("utf-8"))
            live_tag = str((curr_yaml.get("image") or {}).get("tag") or "")
            if live_tag != expected_current_revision:
                return RollbackOutcome(
                    succeeded=False,
                    from_revision=live_tag,
                    message=f"Live release tag {live_tag} != expected {expected_current_revision}",
                )

            # Create revert branch & commit ONLY the single release.yaml file
            branch = f"rollback/{request_name}"
            ref_resp = await client.get(
                f"{self.api_base}/repos/{self.repo}/git/ref/heads/{self.base_branch}"
            )
            base_head = ref_resp.json()["object"]["sha"]
            await client.post(
                f"{self.api_base}/repos/{self.repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": base_head},
            )
            new_content = base64.b64encode(
                yaml.safe_dump(prev_yaml, sort_keys=False).encode("utf-8")
            ).decode("ascii")
            put_resp = await client.put(
                f"{self.api_base}/repos/{self.repo}/contents/{file_path}",
                json={
                    "message": f"revert({service}): {request_name} — {reason[:120]}",
                    "content": new_content,
                    "sha": curr_file["sha"],
                    "branch": branch,
                },
            )
            pr_resp = await client.post(
                f"{self.api_base}/repos/{self.repo}/pulls",
                json={
                    "title": f"Rollback {service} ({request_name})",
                    "head": branch,
                    "base": self.base_branch,
                    "body": f"Approved rollback request `{request_name}`.\n\nReason: {reason}",
                },
            )
            pr_data = pr_resp.json()
            return RollbackOutcome(
                succeeded=pr_resp.status_code in (200, 201),
                from_revision=live_tag,
                to_revision=str((prev_yaml.get("image") or {}).get("tag") or ""),
                to_version=str(prev_yaml.get("version") or ""),
                git_commit=(put_resp.json().get("commit") or {}).get("sha"),
                pr_url=pr_data.get("html_url"),
                message="Guarded revert PR opened via ADR-003 fallback",
            )
