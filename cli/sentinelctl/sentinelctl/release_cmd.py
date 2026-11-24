"""`sentinelctl release` subcommands per BUILD_PROMPT §5.4 and §5.7.

Workflow for `release ship`:
1. Load `releases/catalog.yaml` (5.4 schema: top-level `orders`/`payments`, `good` + `variants`
   with `branch`, `version`, `kind`, `message`; no hardcoded tags).
2. Resolve the branch head commit SHA via `git ls-remote <repo> refs/heads/<branch>`.
3. Verify an image tagged with that SHA exists in the container registry; REFUSE if missing.
4. Write a release ledger row to `chaos-controller` (`POST /v1/faults` / `/v1/ledger/releases`).
5. Update `image.tag` and `version` in `apps/shop/<service>/release.yaml` inside `SENTINELCTL_GITOPS_DIR`.
6. Commit with the exact catalog message and push as the operator.
7. Wait up to 180 s for the Argo CD Application (`shop-<service>`) to reach `Synced` and `Healthy`.

The local docker-compose PB-07 shim path (`--local-shim` / `SENTINELCTL_LOCAL_SHIM=1`) is ONLY
permitted outside Kubernetes and refuses to run when `KUBERNETES_SERVICE_HOST` is set.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
import yaml
from rich.console import Console

release_app = typer.Typer(
    help="Ship release variants from releases/catalog.yaml, restore baselines, or list live versions"
)
console = Console()

ALLOWED_SERVICES = frozenset({"orders", "payments"})
_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def load_catalog(catalog_path: Path) -> dict[str, Any]:
    """Load the §5.4 release catalog."""
    return yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}


def get_service_entry(catalog: dict[str, Any], service: str) -> dict[str, Any]:
    if service not in ALLOWED_SERVICES or service not in catalog:
        raise typer.BadParameter(f"Unknown service in catalog: {service}")
    entry = catalog[service]
    if not isinstance(entry, dict) or "good" not in entry:
        raise typer.BadParameter(f"Invalid catalog entry for service '{service}'")
    return entry


def release_file_for_service(gitops_dir: Path, service: str) -> Path:
    return gitops_dir / "apps" / "shop" / service / "release.yaml"


def argo_app_for_service(service: str) -> str:
    return f"shop-{service}"


def resolve_branch_sha(
    repo: str,
    branch: str,
    *,
    runner: CommandRunner = subprocess.run,
) -> str:
    """Resolve a branch head commit SHA using `git ls-remote <repo> refs/heads/<branch>`."""
    ref = f"refs/heads/{branch}"
    proc = runner(
        ["git", "ls-remote", repo, ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-remote failed for {repo} {ref} (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == ref and _HEX_SHA_RE.match(parts[0]):
            return parts[0].lower()
    raise RuntimeError(f"Branch '{branch}' not found in remote '{repo}'")


def verify_image_exists(
    registry_url: str,
    service: str,
    sha: str,
    *,
    image_prefix: str = "sentinel",
    token: str = "",
    client: httpx.Client | None = None,
) -> bool:
    """Return True iff the OCI registry holds a manifest for `<image_prefix>-<service>:<sha>`."""
    base = registry_url.rstrip("/")
    image_repo = f"{image_prefix}-{service}" if image_prefix else service
    url = f"{base}/v2/{image_repo}/manifests/{sha}"
    headers = {
        "Accept": (
            "application/vnd.oci.image.index.v1+json, "
            "application/vnd.oci.image.manifest.v1+json, "
            "application/vnd.docker.distribution.manifest.v2+json"
        )
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    own_client = client is None
    http = client or httpx.Client(timeout=10.0)
    try:
        resp = http.head(url, headers=headers)
        if resp.status_code == 405:
            resp = http.get(url, headers=headers)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
    finally:
        if own_client:
            http.close()


def update_release_yaml(release_file: Path, *, tag: str, version: str) -> str:
    """Update `image.tag` and `version` in `release.yaml` and return the previous version."""
    prev_data = (
        yaml.safe_load(release_file.read_text(encoding="utf-8")) if release_file.exists() else {}
    )
    data = dict(prev_data or {})
    prev_version = str(data.get("version", ""))
    image_cfg = dict(data.get("image") or {})
    image_cfg["tag"] = tag
    data["image"] = image_cfg
    data["version"] = version
    release_file.parent.mkdir(parents=True, exist_ok=True)
    release_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return prev_version


# Backwards-compatible alias for unit callers
_update_release_yaml = update_release_yaml


def commit_and_push_gitops(
    gitops_dir: Path,
    release_file: Path,
    *,
    message: str,
    runner: CommandRunner = subprocess.run,
) -> str:
    """Stage `release_file`, commit with `message`, and push inside `gitops_dir`."""
    rel = (
        release_file.relative_to(gitops_dir)
        if release_file.is_relative_to(gitops_dir)
        else release_file
    )
    for cmd in (
        ["git", "-C", str(gitops_dir), "add", str(rel)],
        ["git", "-C", str(gitops_dir), "commit", "-m", message],
        ["git", "-C", str(gitops_dir), "push"],
    ):
        proc = runner(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"GitOps command failed ({' '.join(cmd)}): {(proc.stderr or proc.stdout).strip()}"
            )
    rev = runner(
        ["git", "-C", str(gitops_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return rev.stdout.strip() if rev.returncode == 0 else ""


def wait_for_argocd_healthy(
    argocd_url: str,
    app_name: str,
    *,
    token: str = "",
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 5.0,
    client: httpx.Client | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait up to `timeout_seconds` for Argo CD Application `app_name` to reach Synced & Healthy."""
    url = f"{argocd_url.rstrip('/')}/api/v1/applications/{app_name}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = clock() + timeout_seconds
    last_state = "unknown"

    own_client = client is None
    http = client or httpx.Client(timeout=10.0)
    try:
        while True:
            try:
                resp = http.get(url, headers=headers)
                if resp.status_code == 200:
                    body = resp.json()
                    status = body.get("status") or {}
                    sync_status = str((status.get("sync") or {}).get("status", "Unknown"))
                    health_status = str((status.get("health") or {}).get("status", "Unknown"))
                    last_state = f"sync={sync_status}, health={health_status}"
                    if sync_status == "Synced" and health_status == "Healthy":
                        return
                else:
                    last_state = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                last_state = f"error: {exc}"

            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out after {timeout_seconds:.0f}s waiting for Argo CD app "
                    f"'{app_name}' to become Synced and Healthy (last state: {last_state})"
                )
            sleep_fn(min(poll_interval_seconds, remaining))
    finally:
        if own_client:
            http.close()


def write_release_ledger(
    chaos_url: str,
    chaos_token: str,
    *,
    service: str,
    from_version: str,
    to_version: str,
    kind: str,
    tag: str,
    variant: str,
    scenario_id: str | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Record the shipped release in `chaos-controller`'s ground-truth ledger."""
    own_client = client is None
    http = client or httpx.Client(timeout=5.0)
    headers = {"Authorization": f"Bearer {chaos_token}"}
    try:
        resp = http.post(
            f"{chaos_url.rstrip('/')}/v1/faults",
            json={
                "type": "bad_release",
                "service": service,
                "intensity": {
                    "variant": variant,
                    "kind": kind,
                    "from_version": from_version,
                    "version": to_version,
                    "tag": tag,
                },
                "duration_seconds": 1800,
                "actor": "operator",
                "scenario_id": scenario_id,
            },
            headers=headers,
        )
        resp.raise_for_status()
    finally:
        if own_client:
            http.close()


def _notify_local_pb07_shim(
    *,
    service: str,
    tag: str,
    version: str,
    prev_version: str | None,
    message: str,
    pb07_url: str,
    client: httpx.Client | None = None,
) -> None:
    """Notify the docker-compose PB-07 dev shim; strictly forbidden inside Kubernetes."""
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        raise RuntimeError(
            "Refusing to use local PB-07 shim inside Kubernetes (KUBERNETES_SERVICE_HOST is set)."
        )
    argo_app = argo_app_for_service(service)
    own_client = client is None
    http = client or httpx.Client(timeout=5.0)
    try:
        http.post(
            f"{pb07_url.rstrip('/')}/v1/apps/{argo_app}/deploys",
            json={
                "app": argo_app,
                "service": service,
                "revision": tag,
                "version": version,
                "previous_version": prev_version,
                "deployed_at": datetime.now(tz=UTC).isoformat(),
                "git_commit": tag,
                "message": message,
            },
        )
    finally:
        if own_client:
            http.close()


def execute_release_ship(
    *,
    service: str,
    variant: str,
    catalog_path: Path,
    git_repo: str,
    registry_url: str,
    registry_token: str = "",
    image_prefix: str = "sentinel",
    gitops_dir: Path,
    chaos_url: str,
    chaos_token: str,
    argocd_url: str,
    argocd_token: str = "",
    scenario_id: str | None = None,
    timeout_seconds: float = 180.0,
    local_shim: bool = False,
    pb07_url: str = "http://localhost:8095",
    runner: CommandRunner = subprocess.run,
    http_client: httpx.Client | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Full §5.4 / §5.7 release ship workflow, cleanly testable with fake runner/HTTP client."""
    cat = load_catalog(catalog_path)
    svc_cfg = get_service_entry(cat, service)
    var_cfg = (svc_cfg.get("variants") or {}).get(variant)
    if not var_cfg:
        raise typer.BadParameter(f"Unknown variant '{variant}' for service '{service}'")

    branch = str(var_cfg["branch"])
    version = str(var_cfg["version"])
    kind = str(var_cfg["kind"])
    message = str(var_cfg["message"])

    sha = resolve_branch_sha(git_repo, branch, runner=runner)
    if not verify_image_exists(
        registry_url,
        service,
        sha,
        image_prefix=image_prefix,
        token=registry_token,
        client=http_client,
    ):
        raise RuntimeError(
            f"Refusing to ship {service} variant '{variant}': no built image found in "
            f"{registry_url} for commit SHA {sha} (branch {branch})."
        )

    release_file = release_file_for_service(gitops_dir, service)
    prev_version = update_release_yaml(release_file, tag=sha, version=version) or str(
        svc_cfg["good"]["version"]
    )

    if chaos_url:
        write_release_ledger(
            chaos_url,
            chaos_token,
            service=service,
            from_version=prev_version,
            to_version=version,
            kind=kind,
            tag=sha,
            variant=variant,
            scenario_id=scenario_id,
            client=http_client,
        )

    if local_shim:
        _notify_local_pb07_shim(
            service=service,
            tag=sha,
            version=version,
            prev_version=prev_version,
            message=message,
            pb07_url=pb07_url,
            client=http_client,
        )
        gitops_commit = ""
    else:
        gitops_commit = commit_and_push_gitops(
            gitops_dir, release_file, message=message, runner=runner
        )
        wait_for_argocd_healthy(
            argocd_url,
            argo_app_for_service(service),
            token=argocd_token,
            timeout_seconds=timeout_seconds,
            client=http_client,
            sleep_fn=sleep_fn,
            clock=clock,
        )

    return {
        "service": service,
        "variant": variant,
        "branch": branch,
        "sha": sha,
        "from_version": prev_version,
        "version": version,
        "kind": kind,
        "message": message,
        "release_file": str(release_file),
        "gitops_commit": gitops_commit,
    }


def execute_release_restore(
    *,
    service: str,
    catalog_path: Path,
    git_repo: str,
    registry_url: str,
    registry_token: str = "",
    image_prefix: str = "sentinel",
    gitops_dir: Path,
    argocd_url: str,
    argocd_token: str = "",
    timeout_seconds: float = 180.0,
    local_shim: bool = False,
    pb07_url: str = "http://localhost:8095",
    runner: CommandRunner = subprocess.run,
    http_client: httpx.Client | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Restore a service's `good` baseline branch head SHA and version from `catalog.yaml`."""
    cat = load_catalog(catalog_path)
    svc_cfg = get_service_entry(cat, service)
    good = svc_cfg["good"]
    branch = str(good["branch"])
    version = str(good["version"])
    message = str(good["message"])

    sha = resolve_branch_sha(git_repo, branch, runner=runner)
    if not verify_image_exists(
        registry_url,
        service,
        sha,
        image_prefix=image_prefix,
        token=registry_token,
        client=http_client,
    ):
        raise RuntimeError(
            f"Refusing to restore {service}: no built image found in {registry_url} for "
            f"baseline commit SHA {sha} (branch {branch})."
        )

    release_file = release_file_for_service(gitops_dir, service)
    prev_version = update_release_yaml(release_file, tag=sha, version=version)

    if local_shim:
        _notify_local_pb07_shim(
            service=service,
            tag=sha,
            version=version,
            prev_version=prev_version or None,
            message=message,
            pb07_url=pb07_url,
            client=http_client,
        )
        gitops_commit = ""
    else:
        gitops_commit = commit_and_push_gitops(
            gitops_dir, release_file, message=message, runner=runner
        )
        wait_for_argocd_healthy(
            argocd_url,
            argo_app_for_service(service),
            token=argocd_token,
            timeout_seconds=timeout_seconds,
            client=http_client,
            sleep_fn=sleep_fn,
            clock=clock,
        )

    return {
        "service": service,
        "branch": branch,
        "sha": sha,
        "version": version,
        "message": message,
        "release_file": str(release_file),
        "gitops_commit": gitops_commit,
    }


@release_app.command("ship")
def ship_release(
    service: str = typer.Option(..., "--service", "-s", help="orders | payments"),
    variant: str = typer.Option(..., "--variant", "-v", help="itemised-tax | fraud-n1 | rebuild"),
    catalog: Path = typer.Option(Path("releases/catalog.yaml"), "--catalog"),
    scenario_id: str | None = typer.Option(None, "--scenario-id"),
    local_shim: bool = typer.Option(
        False, "--local-shim", help="Use docker-compose PB-07 shim (forbidden in Kubernetes)"
    ),
) -> None:
    """Resolve branch SHA, verify registry image, write ledger, update GitOps, wait for Argo CD."""
    use_shim = (local_shim is True) or os.getenv("SENTINELCTL_LOCAL_SHIM") == "1"
    try:
        res = execute_release_ship(
            service=service,
            variant=variant,
            catalog_path=catalog,
            git_repo=os.getenv("SENTINELCTL_GIT_REPO", "origin"),
            registry_url=os.getenv("SENTINELCTL_REGISTRY_URL", "https://ghcr.io"),
            registry_token=os.getenv("SENTINELCTL_REGISTRY_TOKEN", ""),
            image_prefix=os.getenv("SENTINELCTL_IMAGE_PREFIX", "sentinel"),
            gitops_dir=Path(os.getenv("SENTINELCTL_GITOPS_DIR", "sentinel-gitops")),
            chaos_url=os.getenv("SENTINELCTL_CHAOS_URL", os.getenv("CHAOS_CONTROLLER_URL", "")),
            chaos_token=os.getenv(
                "SENTINELCTL_CHAOS_TOKEN",
                os.getenv("CHAOS_OPERATOR_TOKEN", "dev-chaos-operator-token"),
            ),
            argocd_url=os.getenv("SENTINELCTL_ARGOCD_URL", "http://argocd-server.argocd.svc"),
            argocd_token=os.getenv("SENTINELCTL_ARGOCD_TOKEN", ""),
            scenario_id=scenario_id,
            local_shim=use_shim,
            pb07_url=os.getenv("PB07_CONTROLLER_URL", "http://localhost:8095"),
        )
    except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]Shipped[/green] {res['service']} {res['from_version']} -> "
        f"[bold]{res['version']}[/bold] (sha={res['sha'][:12]}, message={res['message']!r})"
    )


@release_app.command("restore")
def restore_release(
    service: str = typer.Option(..., "--service", "-s", help="orders | payments"),
    catalog: Path = typer.Option(Path("releases/catalog.yaml"), "--catalog"),
    local_shim: bool = typer.Option(
        False, "--local-shim", help="Use docker-compose PB-07 shim (forbidden in Kubernetes)"
    ),
) -> None:
    """Restore baseline good branch SHA and version in apps/shop/<service>/release.yaml."""
    use_shim = (local_shim is True) or os.getenv("SENTINELCTL_LOCAL_SHIM") == "1"
    try:
        res = execute_release_restore(
            service=service,
            catalog_path=catalog,
            git_repo=os.getenv("SENTINELCTL_GIT_REPO", "origin"),
            registry_url=os.getenv("SENTINELCTL_REGISTRY_URL", "https://ghcr.io"),
            registry_token=os.getenv("SENTINELCTL_REGISTRY_TOKEN", ""),
            image_prefix=os.getenv("SENTINELCTL_IMAGE_PREFIX", "sentinel"),
            gitops_dir=Path(os.getenv("SENTINELCTL_GITOPS_DIR", "sentinel-gitops")),
            argocd_url=os.getenv("SENTINELCTL_ARGOCD_URL", "http://argocd-server.argocd.svc"),
            argocd_token=os.getenv("SENTINELCTL_ARGOCD_TOKEN", ""),
            local_shim=use_shim,
            pb07_url=os.getenv("PB07_CONTROLLER_URL", "http://localhost:8095"),
        )
    except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]Restored[/green] {res['service']} to baseline {res['version']} (sha={res['sha'][:12]})"
    )


@release_app.command("list")
def list_releases(
    catalog: Path = typer.Option(Path("releases/catalog.yaml"), "--catalog"),
    gitops_dir: Path = typer.Option(
        Path(os.getenv("SENTINELCTL_GITOPS_DIR", "sentinel-gitops")), "--gitops-dir"
    ),
) -> None:
    """Show catalog entries and live release.yaml tag/version for each shop service."""
    cat = load_catalog(catalog)
    for service in sorted(ALLOWED_SERVICES):
        if service not in cat:
            continue
        rel_file = release_file_for_service(gitops_dir, service)
        live = yaml.safe_load(rel_file.read_text(encoding="utf-8")) if rel_file.exists() else {}
        live_tag = ((live or {}).get("image") or {}).get("tag", "unset")
        live_ver = (live or {}).get("version", "unset")
        variants = ", ".join(sorted((cat[service].get("variants") or {}).keys()))
        console.print(
            f"[bold]{service}[/bold]: live version={live_ver} tag={live_tag} | "
            f"good={cat[service]['good']['version']} ({cat[service]['good']['branch']}) | "
            f"variants=({variants})"
        )
