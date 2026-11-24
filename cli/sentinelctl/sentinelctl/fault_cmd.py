"""`sentinelctl fault` subcommands (FR-CH-01, FR-CH-02, FR-CH-04)."""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

fault_app = typer.Typer(help="Inject, list, and clear runtime faults via chaos-controller")
console = Console()


def parse_duration_seconds(raw: str) -> int:
    m = re.fullmatch(r"(\d+)(s|m|h)?", raw.strip().lower())
    if not m:
        raise typer.BadParameter(f"Invalid duration '{raw}'. Use e.g. 90s, 10m, 1h.")
    val = int(m.group(1))
    unit = m.group(2) or "s"
    mult = {"s": 1, "m": 60, "h": 3600}[unit]
    return val * mult


def parse_intensity(fault_type: str, raw: str) -> dict[str, Any]:
    s = raw.strip().lower()
    if fault_type == "latency":
        ms_match = re.fullmatch(r"(\d+)(?:ms)?", s)
        if not ms_match:
            raise typer.BadParameter("Latency intensity must be milliseconds, e.g. 900ms")
        ms = int(ms_match.group(1))
        if not (50 <= ms <= 10000):
            raise typer.BadParameter("Latency intensity must be between 50ms and 10000ms")
        return {"delay_ms": ms, "share": 1.0}
    if fault_type == "error_rate":
        val = float(s.rstrip("%"))
        share = val / 100.0 if s.endswith("%") or val > 1.0 else val
        if not (0.01 <= share <= 1.0):
            raise typer.BadParameter("error_rate intensity must be in [0.01, 1.0] or 1%..100%")
        return {"share": round(share, 4)}
    if fault_type == "db_exhaustion":
        return {"connections": max(1, int(s))}
    if fault_type == "memory_leak":
        val = float(re.sub(r"mb(?:/m|/min)?$", "", s))
        return {"mb_per_minute": max(1.0, val)}
    if fault_type == "cpu_saturation":
        return {"cores": max(1, min(4, int(s)))}
    raise typer.BadParameter(f"Unsupported runtime fault type: {fault_type}")


def _chaos_headers() -> dict[str, str]:
    token = os.getenv("CHAOS_OPERATOR_TOKEN", "dev-chaos-operator-token")
    return {"Authorization": f"Bearer {token}"}


def _chaos_url() -> str:
    return os.getenv("CHAOS_CONTROLLER_URL", "http://localhost:8010").rstrip("/")


@fault_app.command("inject")
def inject_fault(
    fault_type: str = typer.Argument(
        ..., help="latency | error_rate | db_exhaustion | memory_leak | cpu_saturation"
    ),
    service: str = typer.Option(..., "--service", "-s", help="orders | payments"),
    intensity: str = typer.Option(..., "--intensity", "-i", help="e.g. 900ms, 0.4, 5, 20mb"),
    duration: str = typer.Option("10m", "--duration", "-d", help="e.g. 10m, 300s"),
    actor: str = typer.Option("operator", "--actor"),
    scenario_id: str | None = typer.Option(None, "--scenario-id"),
) -> None:
    """Inject a runtime fault without redeploying the target service (FR-CH-01, FR-CH-02)."""
    parsed_intensity = parse_intensity(fault_type, intensity)
    duration_s = parse_duration_seconds(duration)
    resp = httpx.post(
        f"{_chaos_url()}/v1/faults",
        json={
            "type": fault_type,
            "service": service,
            "intensity": parsed_intensity,
            "duration_seconds": duration_s,
            "actor": actor,
            "scenario_id": scenario_id,
        },
        headers=_chaos_headers(),
        timeout=10.0,
    )
    if resp.status_code >= 300:
        console.print(f"[red]Failed to inject fault:[/red] HTTP {resp.status_code} {resp.text}")
        raise typer.Exit(code=1)
    data = resp.json()
    console.print(
        f"[green]Injected[/green] {data['type']} on [bold]{data['service']}[/bold] "
        f"(id={data['id']}, intensity={data['intensity']}, expires={data['expires_at']})"
    )


@fault_app.command("clear")
def clear_fault(
    fault_id: str | None = typer.Argument(None, help="Specific fault UUID to clear"),
    all_faults: bool = typer.Option(False, "--all", help="Clear all active faults"),
    service: str | None = typer.Option(
        None, "--service", help="Optional service filter with --all"
    ),
) -> None:
    """Clear one active fault or all active faults on demand (FR-CH-02)."""
    if all_faults:
        params = {"service": service} if service else {}
        resp = httpx.delete(
            f"{_chaos_url()}/v1/faults", params=params, headers=_chaos_headers(), timeout=10.0
        )
    elif fault_id:
        resp = httpx.delete(
            f"{_chaos_url()}/v1/faults/{fault_id}", headers=_chaos_headers(), timeout=10.0
        )
    else:
        raise typer.BadParameter("Provide a fault_id or pass --all")

    if resp.status_code >= 300:
        console.print(f"[red]Failed to clear fault:[/red] HTTP {resp.status_code} {resp.text}")
        raise typer.Exit(code=1)
    console.print(f"[green]Cleared:[/green] {resp.json()}")


@fault_app.command("list")
def list_faults(active_only: bool = typer.Option(True, "--active-only/--all")) -> None:
    """List ground-truth ledger entries from chaos-controller."""
    resp = httpx.get(
        f"{_chaos_url()}/v1/ledger",
        params={"active_only": str(active_only).lower()},
        headers=_chaos_headers(),
        timeout=10.0,
    )
    if resp.status_code >= 300:
        console.print(f"[red]Failed to query ledger:[/red] HTTP {resp.status_code} {resp.text}")
        raise typer.Exit(code=1)

    table = Table(title="Chaos Ground-Truth Ledger")
    for col in ("ID", "Type", "Service", "Intensity", "Started", "Expires", "Cleared"):
        table.add_column(col)
    for row in resp.json().get("items", []):
        table.add_row(
            str(row["id"])[:8],
            row["type"],
            row["service"],
            str(row["intensity"]),
            row["started_at"][11:19],
            row["expires_at"][11:19],
            row["cleared_at"][11:19] if row.get("cleared_at") else "-",
        )
    console.print(table)
