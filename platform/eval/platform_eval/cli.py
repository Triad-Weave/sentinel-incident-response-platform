"""`platform-eval` CLI (PB-15): validate, compare (Folio CI gate), and push eval-result v1 files."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import typer
from rich.console import Console

from platform_eval.schema import compare_eval_runs, validate_eval_payload

app = typer.Typer(help="Cohort shared evaluation CLI (PB-15)")
console = Console()


@app.command("validate")
def validate_cmd(report_path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Validate an eval-result v1 JSON report against schema and project metric registry."""
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        result = validate_eval_payload(payload)
    except Exception as exc:
        console.print(f"[red]INVALID[/red] {report_path}: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]VALID[/green] {report_path} "
        f"(project={result.project}, suite={result.suite}, run_id={result.run_id}, passed={result.passed})"
    )


@app.command("compare")
def compare_cmd(
    baseline_path: Path = typer.Argument(..., exists=True, readable=True),
    candidate_path: Path = typer.Argument(..., exists=True, readable=True),
    max_accuracy_drop: float = typer.Option(
        0.02, help="Maximum allowed drop in primary accuracy (0.02 = 2 points on 0..1 scale)"
    ),
    max_cost_increase: float = typer.Option(
        0.20, help="Maximum allowed relative increase in mean_cost_usd (0.20 = 20%)"
    ),
    markdown_out: Path | None = typer.Option(
        None, help="Optional path to write PR-ready Markdown summary"
    ),
) -> None:
    """Compare a candidate eval run against a main-branch baseline (used by Folio CI gate)."""
    baseline = validate_eval_payload(json.loads(baseline_path.read_text(encoding="utf-8")))
    candidate = validate_eval_payload(json.loads(candidate_path.read_text(encoding="utf-8")))
    comparison = compare_eval_runs(
        baseline,
        candidate,
        max_accuracy_drop_points=max_accuracy_drop,
        max_cost_increase_ratio=max_cost_increase,
    )
    if markdown_out is not None:
        markdown_out.write_text(comparison.summary_markdown, encoding="utf-8")

    console.print(comparison.summary_markdown)
    if not comparison.passed:
        raise typer.Exit(code=1)


@app.command("push")
def push_cmd(
    report_path: Path = typer.Argument(..., exists=True, readable=True),
    store_url: str = typer.Option(
        os.getenv("EVAL_STORE_URL", "http://localhost:8096"),
        help="Platform Eval Store base URL",
    ),
    token: str = typer.Option(
        os.getenv("EVAL_STORE_TOKEN", "dev-eval-store-token"),
        help="Bearer token for the Eval Store",
    ),
) -> None:
    """Validate and push an eval-result v1 JSON file to the shared Eval Store."""
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    validate_eval_payload(payload)
    resp = httpx.post(
        f"{store_url.rstrip('/')}/v1/runs",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    )
    if resp.status_code >= 300:
        console.print(f"[red]PUSH FAILED[/red] HTTP {resp.status_code}: {resp.text}")
        raise typer.Exit(code=1)
    console.print(f"[green]PUSHED[/green] {report_path} -> {store_url}")


if __name__ == "__main__":
    app()
