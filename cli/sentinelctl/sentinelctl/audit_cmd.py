"""`sentinelctl audit` subcommands: NFR-05 secret audit and FR-CH-03 ground-truth leak scan."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import typer
from rich.console import Console
from sentinel_common.config import SentinelSettings
from sentinel_common.db import make_engine, make_session_factory
from sentinel_common.models import Alert, InvestigationStep, LlmCall
from sentinel_common.scanners import scan_for_ground_truth_leaks, scan_for_secrets
from sqlalchemy import select

audit_app = typer.Typer(
    help="Audit stored prompts/steps for secret leaks (NFR-05) and ground-truth leaks (FR-CH-03)"
)
console = Console()


async def run_secret_audit(database_url: str, *, canaries: tuple[str, ...]) -> list[dict[str, Any]]:
    engine = make_engine(database_url)
    sf = make_session_factory(engine)
    findings: list[dict[str, Any]] = []
    try:
        async with sf() as sess:
            llm_rows = (await sess.execute(select(LlmCall))).scalars().all()
            for row in llm_rows:
                hits = scan_for_secrets(row.request, canaries=canaries)
                if hits:
                    findings.append({"table": "llm_calls", "id": str(row.id), "hits": hits})

            step_rows = (await sess.execute(select(InvestigationStep))).scalars().all()
            for row in step_rows:
                hits = scan_for_secrets(row.output, canaries=canaries)
                if hits:
                    findings.append(
                        {"table": "investigation_steps", "id": str(row.id), "hits": hits}
                    )
    finally:
        await engine.dispose()
    return findings


async def run_leak_audit(
    database_url: str,
    *,
    fault_ids: list[str] | None = None,
    scenario_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    engine = make_engine(database_url)
    sf = make_session_factory(engine)
    findings: list[dict[str, Any]] = []
    try:
        async with sf() as sess:
            step_rows = (await sess.execute(select(InvestigationStep))).scalars().all()
            for row in step_rows:
                hits = scan_for_ground_truth_leaks(
                    row.output,
                    fault_ids=fault_ids or [],
                    scenario_ids=scenario_ids or [],
                )
                if hits:
                    findings.append(
                        {"table": "investigation_steps", "id": str(row.id), "hits": hits}
                    )

            alert_rows = (await sess.execute(select(Alert))).scalars().all()
            for row in alert_rows:
                hits = scan_for_ground_truth_leaks(
                    {"labels": row.labels, "annotations": row.annotations},
                    fault_ids=fault_ids or [],
                    scenario_ids=scenario_ids or [],
                )
                if hits:
                    findings.append({"table": "alerts", "id": str(row.id), "hits": hits})
    finally:
        await engine.dispose()
    return findings


@audit_app.command("secrets")
def audit_secrets(
    database_url: str = typer.Option(
        os.getenv(
            "SENTINEL_DATABASE_URL",
            "postgresql+asyncpg://sentinel_api:sentinel_api_dev_pw@localhost:5432/sentinel",
        ),
        "--database-url",
    ),
) -> None:
    """Scan every stored llm_calls.request and investigation_steps.output for secrets & canaries (NFR-05)."""
    cfg = SentinelSettings()
    canaries = (cfg.canary_password, cfg.canary_bearer_token)
    hits = asyncio.run(run_secret_audit(database_url, canaries=canaries))
    if hits:
        console.print(f"[red]SECRETS FOUND ({len(hits)}):[/red] {hits}")
        raise typer.Exit(code=1)
    console.print("[green]PASS[/green] secrets_found=0 across all stored prompts and tool steps.")


@audit_app.command("leaks")
def audit_leaks(
    database_url: str = typer.Option(
        os.getenv(
            "SENTINEL_DATABASE_URL",
            "postgresql+asyncpg://sentinel_api:sentinel_api_dev_pw@localhost:5432/sentinel",
        ),
        "--database-url",
    ),
) -> None:
    """Scan every stored tool output and alert payload for ground-truth leakage (FR-CH-03)."""
    hits = asyncio.run(run_leak_audit(database_url))
    if hits:
        console.print(f"[red]GROUND-TRUTH LEAKS FOUND ({len(hits)}):[/red] {hits}")
        raise typer.Exit(code=1)
    console.print("[green]PASS[/green] leaks=0 across all stored tool outputs and alert payloads.")
