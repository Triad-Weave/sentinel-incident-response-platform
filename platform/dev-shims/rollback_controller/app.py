"""Local dev shim for the PB-07 Platform Rollback Controller & Deploy History API.

Owned by the Cluster & Delivery lead (Folio owner) in the real cohort cluster (request R-1).
Implements the exact contract defined in Architecture.pdf pages 18-19:
- GET /v1/apps/{app}/history?limit=  (project read-only bearer token)
- POST /v1/rollbacks                 (enforces project ownership, expectedCurrentRevision check,
                                      reverts one release.yaml file, idempotent on request name)
- POST /v1/apps/{app}/deploys        (records operator deploy commits in local dev mode)
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Platform PB-07 Rollback & Deploy History Shim", version="1.0.0")

READ_TOKEN = os.getenv("DEPLOY_HISTORY_READ_TOKEN", "dev-deploy-history-token")
GITOPS_DIR = Path(os.getenv("GITOPS_DIR", "/gitops"))

PROJECT_APPS = {
    "sentinel": {"shop-orders", "shop-payments"},
    "lumen": {"lumen-api", "lumen-worker"},
    "folio": {"folio-api", "folio-worker"},
}

APP_TO_RELEASE_FILE = {
    "shop-orders": "apps/shop/orders/release.yaml",
    "shop-payments": "apps/shop/payments/release.yaml",
}


class DeployRecord(BaseModel):
    app: str
    service: str
    revision: str
    version: str
    previous_version: str | None = None
    deployed_at: str
    git_commit: str
    message: str


class RollbackRequestBody(BaseModel):
    name: str = Field(min_length=4, pattern=r"^inc-")
    namespace: str = "sentinel"
    app: str
    expectedCurrentRevision: str
    reason: str


# In-memory deploy history & idempotent rollback registry seeded with baseline releases
_DEPLOY_HISTORY: dict[str, list[DeployRecord]] = {
    "shop-orders": [
        DeployRecord(
            app="shop-orders",
            service="orders",
            revision="8b2d004",
            version="1.3.0",
            previous_version="1.2.0",
            deployed_at=datetime.now(tz=UTC).isoformat(),
            git_commit="8b2d004",
            message="chore(orders): baseline release 1.3.0",
        )
    ],
    "shop-payments": [
        DeployRecord(
            app="shop-payments",
            service="payments",
            revision="4c19a21",
            version="2.2.0",
            previous_version="2.1.0",
            deployed_at=datetime.now(tz=UTC).isoformat(),
            git_commit="4c19a21",
            message="chore(payments): baseline release 2.2.0",
        )
    ],
}
_ROLLBACKS: dict[str, dict[str, Any]] = {}


def _require_read_token(authorization: str | None = Header(default=None)) -> None:
    if not authorization or authorization.removeprefix("Bearer ").strip() != READ_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid deploy-history read token")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/apps/{app_name}/history", dependencies=[Depends(_require_read_token)])
async def get_history(app_name: str, limit: int = 5) -> dict[str, Any]:
    if app_name not in _DEPLOY_HISTORY:
        raise HTTPException(status_code=404, detail=f"Unknown app: {app_name}")
    items = list(reversed(_DEPLOY_HISTORY[app_name]))[: max(1, min(limit, 20))]
    return {"app": app_name, "deploys": [d.model_dump() for d in items]}


@app.post("/v1/apps/{app_name}/deploys")
async def record_deploy(app_name: str, record: DeployRecord) -> dict[str, Any]:
    if app_name not in _DEPLOY_HISTORY:
        raise HTTPException(status_code=404, detail=f"Unknown app: {app_name}")
    _DEPLOY_HISTORY[app_name].append(record)
    return {"recorded": True, "count": len(_DEPLOY_HISTORY[app_name])}


@app.post("/v1/rollbacks")
async def execute_rollback(req: RollbackRequestBody) -> dict[str, Any]:
    # Idempotent on request name
    if req.name in _ROLLBACKS:
        return _ROLLBACKS[req.name]

    allowed = PROJECT_APPS.get(req.namespace, set())
    if req.app not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"App '{req.app}' does not belong to namespace '{req.namespace}'",
        )

    history = _DEPLOY_HISTORY.get(req.app, [])
    if len(history) < 2:
        status = {
            "phase": "Failed",
            "fromRevision": req.expectedCurrentRevision,
            "toRevision": None,
            "gitCommit": None,
            "message": "No previous release revision to revert to",
        }
        _ROLLBACKS[req.name] = status
        return status

    current = history[-1]
    previous = history[-2]
    if current.revision != req.expectedCurrentRevision:
        status = {
            "phase": "Failed",
            "fromRevision": current.revision,
            "toRevision": None,
            "gitCommit": None,
            "message": (
                f"Live revision {current.revision} does not match "
                f"expectedCurrentRevision {req.expectedCurrentRevision}"
            ),
        }
        _ROLLBACKS[req.name] = status
        return status

    # Update single release.yaml file if gitops volume is mounted
    rel_path = APP_TO_RELEASE_FILE.get(req.app)
    if rel_path:
        target_file = GITOPS_DIR / rel_path
        if target_file.exists():
            data = yaml.safe_load(target_file.read_text(encoding="utf-8")) or {}
            data.setdefault("image", {})["tag"] = previous.revision
            data["version"] = previous.version
            target_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    revert_sha = hashlib.sha1(f"{req.name}:{previous.revision}".encode()).hexdigest()[:7]
    history.append(
        DeployRecord(
            app=req.app,
            service=current.service,
            revision=previous.revision,
            version=previous.version,
            previous_version=current.version,
            deployed_at=datetime.now(tz=UTC).isoformat(),
            git_commit=revert_sha,
            message=f"revert({current.service}): rollback to {previous.version} ({req.reason})",
        )
    )
    status = {
        "phase": "Succeeded",
        "fromRevision": current.revision,
        "toRevision": previous.revision,
        "toVersion": previous.version,
        "gitCommit": revert_sha,
        "message": "reverted 1 deploy commit; Synced and Healthy",
    }
    _ROLLBACKS[req.name] = status
    return status


@app.get("/v1/rollbacks/{name}")
async def get_rollback(name: str) -> dict[str, Any]:
    if name not in _ROLLBACKS:
        raise HTTPException(status_code=404, detail="RollbackRequest not found")
    return _ROLLBACKS[name]
