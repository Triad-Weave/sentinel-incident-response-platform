"""`sentinelctl eval run` — one-command end-to-end evaluation harness (FR-EV-01..04, PB-15)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
import yaml
from rich.console import Console
from sentinel_common.scanners import scan_for_ground_truth_leaks, scan_for_secrets
from sentinel_worker.llm_client import compute_prompt_version

from sentinelctl.fault_cmd import parse_duration_seconds
from sentinelctl.scorer import ScenarioOutcome, build_eval_report, save_report_files

eval_app = typer.Typer(
    help="Run end-to-end Sentinel evaluation scenarios and publish PB-15 reports"
)
console = Console()


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        )
        return out.decode("utf-8").strip() or "working"
    except Exception:
        return "working"


def load_scenarios(scenarios_dir: Path, selection: str) -> list[dict[str, Any]]:
    files = sorted(scenarios_dir.glob("S*.yaml"))
    loaded = [yaml.safe_load(f.read_text(encoding="utf-8")) for f in files]
    if selection.strip().lower() == "all":
        return loaded
    wanted = {s.strip().upper() for s in selection.split(",") if s.strip()}
    return [
        sc
        for sc in loaded
        if sc["id"].upper() in wanted or sc["id"].split("-")[0].upper() in wanted
    ]


def _call_llm_judge(
    *,
    gateway_url: str,
    gateway_key: str,
    judge_model: str,
    judge_prompt: str,
    scenario: dict[str, Any],
    finding_body: dict[str, Any],
) -> tuple[int | None, str | None, float]:
    user_payload = {
        "ground_truth": {
            "service": scenario["expect"]["service"],
            "fault_type": scenario["expect"]["fault_type"],
            "action": scenario["expect"]["action"],
        },
        "judge_reference": scenario.get("judge_reference", ""),
        "hypothesis": finding_body.get("hypothesis", ""),
        "evidence_claims": finding_body.get("evidence", []),
    }
    try:
        resp = httpx.post(
            f"{gateway_url.rstrip('/')}/chat/completions",
            json={
                "model": judge_model,
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": judge_prompt},
                    {"role": "user", "content": json.dumps(user_payload)},
                ],
            },
            headers={
                "Authorization": f"Bearer {gateway_key}",
                "X-LLM-Feature": "judge",
            },
            timeout=30.0,
        )
        if resp.status_code >= 300:
            return None, f"judge HTTP {resp.status_code}", 0.0
        data = resp.json()
        cost = float((data.get("gateway") or {}).get("cost_usd", 0.0))
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return int(parsed["score"]), str(parsed.get("reason", "")), cost
    except Exception as exc:
        return None, f"judge error: {exc}", 0.0


def _wait_for_steady_state(
    prometheus_url: str,
    *,
    baseline_minutes: int,
    timeout_s: float = 180.0,
) -> None:
    """Gate before injection: error ratio < 1% and p95 < 250ms (Architecture p.39)."""
    if baseline_minutes <= 0:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            err_r = httpx.get(
                f"{prometheus_url}/api/v1/query",
                params={
                    "query": 'max(sum by (service) (rate(http_server_request_duration_seconds_count{namespace="shop",http_response_status_code=~"5.."}[1m])) / clamp_min(sum by (service) (rate(http_server_request_duration_seconds_count{namespace="shop"}[1m])), 0.001))'
                },
                timeout=5.0,
            ).json()
            p95_r = httpx.get(
                f"{prometheus_url}/api/v1/query",
                params={
                    "query": 'max(histogram_quantile(0.95, sum by (le, service) (rate(http_server_request_duration_seconds_bucket{namespace="shop"}[1m]))))'
                },
                timeout=5.0,
            ).json()
            err_res = err_r.get("data", {}).get("result") or []
            p95_res = p95_r.get("data", {}).get("result") or []
            err_val = float(err_res[0]["value"][1]) if err_res else 0.0
            p95_val = float(p95_res[0]["value"][1]) if p95_res else 0.0
            if err_val < 0.01 and p95_val < 0.25:
                return
        except Exception:
            return
        time.sleep(5.0)


@eval_app.command("run")
def run_eval(
    scenarios: str = typer.Option(
        "all", "--scenarios", help="'all' or comma-separated IDs like S01,S03"
    ),
    repeat: int = typer.Option(1, "--repeat", min=1, max=5),
    execute_actions: bool = typer.Option(False, "--execute-actions"),
    enable_judge: bool = typer.Option(True, "--judge/--no-judge"),
    restart_enabled: bool = typer.Option(True, "--restart-enabled/--no-restart-enabled"),
    push_store: bool = typer.Option(False, "--push/--no-push"),
    scenarios_dir: Path = typer.Option(Path("eval/scenarios"), "--scenarios-dir"),
    reports_dir: Path = typer.Option(Path("eval/reports"), "--reports-dir"),
    api_url: str = typer.Option(
        os.getenv("SENTINEL_API_URL", "http://localhost:8000"), "--api-url"
    ),
    chaos_url: str = typer.Option(
        os.getenv("CHAOS_CONTROLLER_URL", "http://localhost:8010"), "--chaos-url"
    ),
    prometheus_url: str = typer.Option(
        os.getenv("PROMETHEUS_URL", "http://localhost:9090"), "--prometheus-url"
    ),
    gateway_url: str = typer.Option(
        os.getenv("LLM_GATEWAY_URL", "http://localhost:8090/v1"), "--gateway-url"
    ),
) -> None:
    """Run Sentinel scenarios end to end, score, run leak/secret scans, and write PB-15 reports."""
    selected = load_scenarios(scenarios_dir, scenarios)
    if not selected:
        console.print("[red]No matching scenarios found.[/red]")
        raise typer.Exit(code=1)

    api_token = os.getenv("ALERTMANAGER_WEBHOOK_TOKEN", "dev-alertmanager-webhook-token")
    chaos_token = os.getenv("CHAOS_OPERATOR_TOKEN", "dev-chaos-operator-token")
    gw_key = os.getenv("LLM_GATEWAY_API_KEY", "dev-sentinel-gateway-key")
    agent_model = os.getenv("SENTINEL_AGENT_MODEL", "gpt-4o-mini")
    judge_model = os.getenv("SENTINEL_JUDGE_MODEL", "gpt-4o")
    canaries = (
        os.getenv("CANARY_PASSWORD", "canary-pw-sentinel-9f8e7d6c"),
        os.getenv("CANARY_BEARER_TOKEN", "canary-tok-sentinel-a1b2c3d4e5f6"),
    )

    api_headers = {"Authorization": f"Bearer {api_token}"}
    chaos_headers = {"Authorization": f"Bearer {chaos_token}"}

    # 1. Preflight checks (Architecture p.39)
    console.print("[bold]Preflight checks...[/bold]")
    api_health = httpx.get(f"{api_url.rstrip('/')}/healthz", timeout=5.0)
    chaos_health = httpx.get(f"{chaos_url.rstrip('/')}/healthz", timeout=5.0)
    if api_health.status_code != 200 or chaos_health.status_code != 200:
        console.print("[red]Preflight failed: Sentinel API or Chaos Controller unhealthy.[/red]")
        raise typer.Exit(code=1)

    open_incs = (
        httpx.get(
            f"{api_url.rstrip('/')}/api/v1/incidents",
            params={"status": "open"},
            headers=api_headers,
            timeout=10.0,
        )
        .json()
        .get("items", [])
    )
    if open_incs:
        console.print(
            f"[red]Preflight failed: {len(open_incs)} open incident(s) exist. Resolve first.[/red]"
        )
        raise typer.Exit(code=1)

    active_faults = (
        httpx.get(
            f"{chaos_url.rstrip('/')}/v1/ledger",
            params={"active_only": "true"},
            headers=chaos_headers,
            timeout=10.0,
        )
        .json()
        .get("items", [])
    )
    if active_faults:
        console.print(
            f"[red]Preflight failed: {len(active_faults)} active fault(s) exist. Clear first.[/red]"
        )
        raise typer.Exit(code=1)

    required_budget = len(selected) * repeat * 0.35
    try:
        budget_resp = httpx.get(
            f"{gateway_url.rstrip('/')}/budget",
            headers={"Authorization": f"Bearer {gw_key}"},
            timeout=5.0,
        )
        if budget_resp.status_code == 200:
            remaining = float(budget_resp.json().get("remaining_usd", required_budget))
            if remaining < required_budget:
                console.print(
                    f"[red]Preflight failed: remaining LLM budget ${remaining:.2f} < needed ${required_budget:.2f}[/red]"
                )
                raise typer.Exit(code=1)
    except Exception:
        pass

    judge_prompt = Path("eval/judge/prompt.md").read_text(encoding="utf-8") if enable_judge else ""
    outcomes: list[ScenarioOutcome] = []
    # Keep scenario-to-incident map strictly in the harness's local results file (FR-CH-03!)
    local_map: list[dict[str, Any]] = []

    for rep in range(1, repeat + 1):
        for sc in selected:
            sc_id = sc["id"]
            console.print(f"\n[cyan]=== Repeat {rep}/{repeat} · {sc_id} ===[/cyan]")
            _wait_for_steady_state(
                prometheus_url,
                baseline_minutes=int((sc.get("setup") or {}).get("baseline_minutes", 0)),
            )

            injected_fault_ids: list[str] = []
            injected_at = datetime.now(tz=UTC)

            for step in sc.get("inject", []):
                after_s = parse_duration_seconds(str(step.get("after", "0s")))
                if after_s > 0:
                    time.sleep(after_s)
                if step["kind"] == "fault":
                    r = httpx.post(
                        f"{chaos_url.rstrip('/')}/v1/faults",
                        json={
                            "type": step["type"],
                            "service": step["service"],
                            "intensity": step.get("intensity", {}),
                            "duration_seconds": parse_duration_seconds(
                                str(step.get("duration", "10m"))
                            ),
                            "actor": "eval-harness",
                            "scenario_id": sc_id,
                        },
                        headers=chaos_headers,
                        timeout=10.0,
                    )
                    if r.status_code < 300:
                        injected_fault_ids.append(str(r.json()["id"]))
                elif step["kind"] == "release":
                    from sentinelctl.release_cmd import ship_release

                    ship_release(
                        service=step["service"],
                        variant=step["variant"],
                        catalog=Path("releases/catalog.yaml"),
                        scenario_id=sc_id,
                    )

            # Wait up to 240s for a new open incident
            incident_id: str | None = None
            wait_deadline = time.monotonic() + 240.0
            while time.monotonic() < wait_deadline:
                items = (
                    httpx.get(
                        f"{api_url.rstrip('/')}/api/v1/incidents",
                        params={"status": "open"},
                        headers=api_headers,
                        timeout=10.0,
                    )
                    .json()
                    .get("items", [])
                )
                if items:
                    incident_id = items[0]["id"]
                    break
                time.sleep(3.0)

            detail: dict[str, Any] | None = None
            if incident_id:
                local_map.append({"scenario_id": sc_id, "repeat": rep, "incident_id": incident_id})
                find_deadline = time.monotonic() + 300.0
                while time.monotonic() < find_deadline:
                    detail = httpx.get(
                        f"{api_url.rstrip('/')}/api/v1/incidents/{incident_id}",
                        headers=api_headers,
                        timeout=10.0,
                    ).json()
                    st = detail["incident"]["status"]
                    if st != "Investigating":
                        break
                    time.sleep(3.0)

            expected_action = (
                sc["expect"].get("action_if_restart_enabled", sc["expect"]["action"])
                if restart_enabled
                else sc["expect"]["action"]
            )

            if not detail or not detail.get("finding"):
                outcomes.append(
                    ScenarioOutcome(
                        scenario_id=sc_id,
                        repeat=rep,
                        expected_service=sc["expect"]["service"],
                        expected_fault_type=sc["expect"]["fault_type"],
                        expected_action=expected_action,
                        suspected_service=None,
                        fault_type=None,
                        proposed_action_type=None,
                        proposed_action_service=None,
                        fault_to_alert_s=None,
                        alert_to_finding_s=None,
                        tool_calls=int(
                            (detail or {}).get("investigation", {}).get("tool_calls", 0)
                        ),
                        input_tokens=int(
                            (detail or {}).get("investigation", {}).get("input_tokens", 0)
                        ),
                        output_tokens=int(
                            (detail or {}).get("investigation", {}).get("output_tokens", 0)
                        ),
                        cost_usd=float(
                            (detail or {}).get("investigation", {}).get("cost_usd", 0.0)
                        ),
                        limit_reached=False,
                        failed=True,
                    )
                )
            else:
                inc_obj = detail["incident"]
                inv_obj = detail["investigation"] or {}
                find_obj = detail["finding"]
                body = find_obj["body"]

                started_dt = datetime.fromisoformat(inc_obj["started_at"])
                opened_dt = datetime.fromisoformat(inc_obj["opened_at"])
                finding_dt = (
                    datetime.fromisoformat(inc_obj["finding_at"])
                    if inc_obj.get("finding_at")
                    else datetime.now(tz=UTC)
                )
                fta_s = max(0.0, (started_dt - injected_at).total_seconds())
                atf_s = max(0.0, (finding_dt - opened_dt).total_seconds())

                # Leak scan & secret audit on stored steps and alerts
                leak_hits: list[str] = []
                secret_hits: list[str] = []
                for al in detail.get("alerts", []):
                    leak_hits.extend(
                        scan_for_ground_truth_leaks(
                            al, fault_ids=injected_fault_ids, scenario_ids=[sc_id]
                        )
                    )
                for st_meta in inv_obj.get("steps", []):
                    step_full = httpx.get(
                        f"{api_url.rstrip('/')}/api/v1/investigations/{inv_obj['id']}/steps/{st_meta['seq']}",
                        headers=api_headers,
                        timeout=10.0,
                    ).json()
                    leak_hits.extend(
                        scan_for_ground_truth_leaks(
                            step_full.get("output"),
                            fault_ids=injected_fault_ids,
                            scenario_ids=[sc_id],
                        )
                    )
                    secret_hits.extend(scan_for_secrets(step_full.get("output"), canaries=canaries))

                j_score, j_reason, j_cost = (None, None, 0.0)
                if enable_judge:
                    j_score, j_reason, j_cost = _call_llm_judge(
                        gateway_url=gateway_url,
                        gateway_key=gw_key,
                        judge_model=judge_model,
                        judge_prompt=judge_prompt,
                        scenario=sc,
                        finding_body=body,
                    )

                outcomes.append(
                    ScenarioOutcome(
                        scenario_id=sc_id,
                        repeat=rep,
                        expected_service=sc["expect"]["service"],
                        expected_fault_type=sc["expect"]["fault_type"],
                        expected_action=expected_action,
                        suspected_service=find_obj["suspected_service"],
                        fault_type=find_obj["fault_type"],
                        proposed_action_type=find_obj["action_type"],
                        proposed_action_service=find_obj["action_service"],
                        fault_to_alert_s=fta_s,
                        alert_to_finding_s=atf_s,
                        tool_calls=int(inv_obj.get("tool_calls", 0)),
                        input_tokens=int(inv_obj.get("input_tokens", 0)),
                        output_tokens=int(inv_obj.get("output_tokens", 0)),
                        cost_usd=float(inv_obj.get("cost_usd", 0.0)),
                        limit_reached=bool(find_obj.get("limit_reached", False)),
                        failed=inv_obj.get("status") == "failed",
                        judge_score=j_score,
                        judge_reason=j_reason,
                        judge_cost_usd=j_cost,
                        leak_hits=sorted(set(leak_hits)),
                        secret_hits=sorted(set(secret_hits)),
                        hypothesis=str(body.get("hypothesis", "")),
                        incident_short_id=inc_obj["short_id"],
                    )
                )

                if (
                    execute_actions
                    and detail.get("action_request")
                    and detail["action_request"]["status"] == "proposed"
                ):
                    httpx.post(
                        f"{api_url.rstrip('/')}/api/v1/actions/{detail['action_request']['id']}/approve",
                        json={"reason": f"eval-bot automated execution for {sc_id}"},
                        headers=api_headers,
                        timeout=10.0,
                    )

            # Cleanup faults, restore releases, resolve open incident
            cleanup = sc.get("cleanup") or {}
            if cleanup.get("clear_faults"):
                httpx.delete(
                    f"{chaos_url.rstrip('/')}/v1/faults", headers=chaos_headers, timeout=10.0
                )
            if cleanup.get("restore_release"):
                from sentinelctl.release_cmd import restore_release

                for step in sc.get("inject", []):
                    if step["kind"] == "release":
                        restore_release(
                            service=step["service"], catalog=Path("releases/catalog.yaml")
                        )
            if incident_id:
                httpx.post(
                    f"{api_url.rstrip('/')}/api/v1/incidents/{incident_id}/resolve",
                    headers=api_headers,
                    timeout=10.0,
                )

    run_id = f"sentinel-{datetime.now(tz=UTC).strftime('%Y%m%d-%H%M%S')}"
    report = build_eval_report(
        outcomes,
        run_id=run_id,
        git_sha=_git_sha(),
        prompt_version=compute_prompt_version(),
        model=agent_model,
        judge_model=judge_model if enable_judge else None,
        repeats=repeat,
    )
    json_path, md_path = save_report_files(report, reports_dir)
    map_path = reports_dir / f"{run_id}.local-map.json"
    map_path.write_text(json.dumps(local_map, indent=2) + "\n", encoding="utf-8")

    console.print(f"\n[bold green]Saved reports:[/bold green] {json_path} & {md_path}")
    if push_store:
        from platform_eval.cli import push_cmd

        push_cmd(report_path=json_path)

    if not report.passed:
        console.print(f"[red]Eval run FAILED:[/red] {report.failure_reasons}")
        raise typer.Exit(code=1)
