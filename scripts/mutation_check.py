"""12-Bug Mutation Verification Harness (Verification Plan Section 4, P5).

Plants 12 known security/correctness defects ONE AT A TIME, runs the exact named
verification test (`T05`..`T39`), asserts that the test FAILS (kills the mutant),
and ALWAYS restores the original source file in a `finally` block.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_BIN_DIR = (
    (REPO_ROOT / ".venv/bin")
    if (REPO_ROOT / ".venv/bin/pytest").exists()
    else Path(sys.executable).parent
)
PYTEST_BIN = str(_BIN_DIR / "pytest")


@dataclass(frozen=True)
class Mutant:
    id: str
    description: str
    file_rel: str
    original: str
    mutated: str
    target_test: str


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        id="M01",
        description="Break Luhn card-number redaction",
        file_rel="sentinel/common/redaction.py",
        original="return checksum % 10 == 0",
        mutated="return False",
        target_test="tests/verification/test_t13_t21_mcp_and_safety.py::test_T19_redactor_strips_all_secret_classes_and_luhn_cards",
    ),
    Mutant(
        id="M02",
        description='Drop fixed namespace="shop" pin in PromQL query builder',
        file_rel="sentinel/mcp_server/sentinel_mcp/queries.py",
        original='base = f\'namespace="shop",service="{service}"\'',
        mutated="base = f'service=\"{service}\"'",
        target_test="tests/verification/test_t13_t21_mcp_and_safety.py::test_T13_all_14_promql_and_logql_templates_pin_shop_namespace",
    ),
    Mutant(
        id="M03",
        description="Return unsanitized pod dicts (leaking env/args/annotations) in get_pod_status",
        file_rel="sentinel/mcp_server/sentinel_mcp/tools.py",
        original="clean_pods = [\n            {k: p.get(k) for k in ALLOWED_POD_FIELDS if k in p}\n            for p in raw_pods\n            if isinstance(p, dict)\n        ]",
        mutated="clean_pods = [p for p in raw_pods if isinstance(p, dict)]",
        target_test="tests/verification/test_t13_t21_mcp_and_safety.py::test_T16_get_pod_status_returns_status_only_never_env_args_annotations",
    ),
    Mutant(
        id="M04",
        description="Bypass 15-minute recent-deploy rollback guardrail",
        file_rel="sentinel/worker/sentinel_worker/agent.py",
        original="if not self._has_recent_deploy(recent_deploys_cache, final_action_service, alert_started_at):",
        mutated="if False:",
        target_test="tests/verification/test_t22_t29_agent_and_limits.py::test_T23_guardrail_downgrades_rollback_without_recent_deploy",
    ),
    Mutant(
        id="M05",
        description="Disable max_tool_calls hard budget guard",
        file_rel="sentinel/worker/sentinel_worker/agent.py",
        original="if for_tool and inv.tool_calls >= self.limits.max_tool_calls:",
        mutated="if False:",
        target_test="tests/verification/test_t22_t29_agent_and_limits.py::test_T24_tool_call_hard_limit_forces_finding_and_caps_confidence",
    ),
    Mutant(
        id="M06",
        description="Bypass responder RBAC check (allowing viewer to approve actions)",
        file_rel="sentinel/api/sentinel_api/deps.py",
        original='if user.get("role") != "responder":',
        mutated="if False:",
        target_test="tests/verification/test_t30_t40_api_executors_e2e.py::test_T30_rbac_viewer_blocked_from_all_mutating_endpoints",
    ),
    Mutant(
        id="M07",
        description="Disable Alertmanager (fingerprint, starts_at) idempotency dedup",
        file_rel="sentinel/api/sentinel_api/routes/alerts.py",
        original="# Idempotency: repeat firing alert with same (fingerprint, starts_at) is a no-op\n            if existing_alert is not None:",
        mutated="# MUTATED\n            if False:",
        target_test="tests/verification/test_t30_t40_api_executors_e2e.py::test_T31_alert_dedup_attach_banner_and_10m_parent_link",
    ),
    Mutant(
        id="M08",
        description="Disable 15-minute action approval expiry guard",
        file_rel="sentinel/api/sentinel_api/routes/actions.py",
        original="if _as_utc(action.expires_at) < now:",
        mutated="if False:",
        target_test="tests/verification/test_t30_t40_api_executors_e2e.py::test_T32_action_reason_validation_expiry_and_state_transitions",
    ),
    Mutant(
        id="M09",
        description="Remove conditional status=='approved' check in rollback-executor claim",
        file_rel="sentinel/rollback_executor/rollback_executor/main.py",
        original='ActionRequest.status == "approved",',
        mutated='ActionRequest.status.in_(["approved", "failed", "succeeded", "executing"]),',
        target_test="tests/verification/test_t30_t40_api_executors_e2e.py::test_T36_executors_idempotent_allowlist_and_failure_recovery",
    ),
    Mutant(
        id="M10",
        description="Drop pod_started_at <= f.started_at check in faultkit db_exhaustion",
        file_rel="services/faultkit/faultkit/resources.py",
        original='if f.fault_type == "db_exhaustion" and self.store.pod_started_at <= f.started_at',
        mutated='if f.fault_type == "db_exhaustion"',
        target_test="tests/verification/test_t01_t12_target_and_obs.py::test_T05_faultkit_db_exhaustion_and_restarted_pod_recovery",
    ),
    Mutant(
        id="M11",
        description="Allow Sentinel eval report passed=True even when unsafe_actions > 0",
        file_rel="cli/sentinelctl/sentinelctl/scorer.py",
        original="passed = (unsafe_count == 0) and (total_leaks == 0) and (total_secrets == 0) and (len(failure_reasons) == 0)",
        mutated="passed = True",
        target_test="tests/verification/test_t30_t40_api_executors_e2e.py::test_T38_ten_scenarios_deterministic_scorer_and_eval_store",
    ),
    Mutant(
        id="M12",
        description="Disable Folio CI 2-point field_accuracy drop gate",
        file_rel="platform/eval/platform_eval/schema.py",
        original="if b_acc - c_acc > drop_limit + 1e-9:",
        mutated="if False:",
        target_test="tests/verification/test_t30_t40_api_executors_e2e.py::test_T39_llm_gateway_budget_and_lumen_folio_pb15_contracts",
    ),
)


def _assert_clean_targets() -> None:
    """Refuse to run if not in a clean git worktree or if any target file has uncommitted changes."""
    in_git = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if in_git.returncode != 0 or in_git.stdout.strip() != "true":
        raise RuntimeError(
            "Refusing to run in-place mutation script outside a git repository. "
            "Use `mutmut run` (`make verify-mutation`) which mutates isolated copies in `mutants/`."
        )

    dirty_files: list[str] = []
    for rel in sorted({m.file_rel for m in MUTANTS}):
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", rel],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            dirty_files.append(rel)

    if dirty_files:
        raise RuntimeError(
            "Refusing to mutate target files with uncommitted changes: "
            + ", ".join(dirty_files)
            + ". Commit or stash changes first, or use `mutmut run` (`make verify-mutation`)."
        )


def run_mutations() -> tuple[list[dict[str, object]], bool]:
    _assert_clean_targets()
    results: list[dict[str, object]] = []
    all_killed = True

    for m in MUTANTS:
        path = REPO_ROOT / m.file_rel
        original_content = path.read_text(encoding="utf-8")
        if m.original not in original_content:
            results.append(
                {
                    "id": m.id,
                    "description": m.description,
                    "target_test": m.target_test.split("::")[-1],
                    "killed": False,
                    "reason": "Mutation anchor string not found in file",
                }
            )
            all_killed = False
            continue

        mutated_content = original_content.replace(m.original, m.mutated, 1)

        def _restore_and_exit(
            _signum: int,
            _frame: object,
            _path: Path = path,
            _content: str = original_content,
        ) -> None:
            _path.write_text(_content, encoding="utf-8")
            raise SystemExit(130)

        prev_int = signal.signal(signal.SIGINT, _restore_and_exit)
        prev_term = signal.signal(signal.SIGTERM, _restore_and_exit)
        try:
            path.write_text(mutated_content, encoding="utf-8")
            proc = subprocess.run(
                [PYTEST_BIN, "-q", "--no-cov", m.target_test],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            killed = proc.returncode != 0
        finally:
            # Always restore byte-for-byte original source!
            path.write_text(original_content, encoding="utf-8")
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)

        if not killed:
            all_killed = False
        results.append(
            {
                "id": m.id,
                "description": m.description,
                "target_test": m.target_test.split("::")[-1],
                "killed": killed,
            }
        )

    return results, all_killed


def main() -> int:
    results, all_killed = run_mutations()
    killed_cnt = sum(1 for r in results if r["killed"])
    if "--json" in sys.argv:
        print(
            json.dumps({"killed": killed_cnt, "total": len(results), "results": results}, indent=2)
        )
    else:
        for r in results:
            status = "KILLED" if r["killed"] else "SURVIVED"
            print(f"[{status}] {r['id']}: {r['description']} -> {r['target_test']}")
        print(f"\nMutation score: {killed_cnt}/{len(results)} killed")
    return 0 if all_killed else 1


if __name__ == "__main__":
    raise SystemExit(main())
