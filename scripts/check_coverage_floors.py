"""Enforce docs/verification-plan.md coverage floors (Step 1):

- Overall line coverage >= 80.0%
- Branch coverage >= 90.0% on EVERY ONE of the 10 core modules:
  redaction, onset, clustering, queries, guardrails, budget,
  scoring, correlation, finding schema, leakscan.

Exits non-zero if ANY floor is missed. Never omits or weakens checks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OVERALL_LINE_FLOOR = 80.0
CORE_BRANCH_FLOOR = 90.0

CORE_MODULES: list[tuple[str, str]] = [
    ("redaction", "sentinel/common/redaction.py"),
    ("onset", "sentinel/mcp_server/sentinel_mcp/onset.py"),
    ("clustering", "sentinel/mcp_server/sentinel_mcp/log_cluster.py"),
    ("queries", "sentinel/mcp_server/sentinel_mcp/queries.py"),
    ("guardrails", "sentinel/worker/sentinel_worker/guardrails.py"),
    ("budget", "sentinel/worker/sentinel_worker/budget.py"),
    ("scoring", "cli/sentinelctl/sentinelctl/scorer.py"),
    ("correlation", "sentinel/api/sentinel_api/correlation.py"),
    ("finding schema", "sentinel/common/schemas.py"),
    ("leakscan", "sentinel/common/scanners.py"),
]


def main(argv: list[str]) -> int:
    cov_path = Path(argv[1]) if len(argv) > 1 else Path("coverage.json")
    if not cov_path.exists():
        print(f"ERROR: coverage file not found: {cov_path}", file=sys.stderr)
        return 2

    data = json.loads(cov_path.read_text(encoding="utf-8"))
    totals = data["totals"]
    num_stmts = int(totals["num_statements"])
    cov_lines = int(totals["covered_lines"])
    num_br = int(totals.get("num_branches", 0))
    cov_br = int(totals.get("covered_branches", 0))
    overall_line_pct = (cov_lines / num_stmts * 100.0) if num_stmts else 0.0
    overall_branch_pct = (cov_br / num_br * 100.0) if num_br else 100.0

    failures: list[str] = []
    if overall_line_pct < OVERALL_LINE_FLOOR:
        failures.append(
            f"Overall line coverage {overall_line_pct:.2f}% < floor {OVERALL_LINE_FLOOR:.1f}%"
        )

    print(
        f"Overall line coverage: {overall_line_pct:.2f}% ({cov_lines}/{num_stmts}) "
        f"[floor >= {OVERALL_LINE_FLOOR:.1f}%] | "
        f"Overall branch coverage: {overall_branch_pct:.2f}% ({cov_br}/{num_br})"
    )
    print("")
    print(f"{'Plan module':<16} {'File':<48} {'Line %':>8} {'Branch %':>10} {'Status':>8}")
    print("-" * 96)

    files = data.get("files", {})
    for plan_name, rel_path in CORE_MODULES:
        entry = files.get(rel_path)
        if entry is None:
            failures.append(f"Core module '{plan_name}' ({rel_path}) missing from coverage report")
            print(f"{plan_name:<16} {rel_path:<48} {'MISSING':>8} {'MISSING':>10} {'FAIL':>8}")
            continue
        summary = entry["summary"]
        m_stmts = int(summary["num_statements"])
        m_lines = int(summary["covered_lines"])
        m_br = int(summary.get("num_branches", 0))
        m_cov_br = int(summary.get("covered_branches", 0))
        line_pct = (m_lines / m_stmts * 100.0) if m_stmts else 100.0
        br_pct = (m_cov_br / m_br * 100.0) if m_br else 100.0
        ok = br_pct >= CORE_BRANCH_FLOOR
        if not ok:
            failures.append(
                f"Core module '{plan_name}' ({rel_path}) branch coverage {br_pct:.1f}% "
                f"({m_cov_br}/{m_br}) < floor {CORE_BRANCH_FLOOR:.1f}%"
            )
        print(
            f"{plan_name:<16} {rel_path:<48} "
            f"{line_pct:>7.1f}% {br_pct:>7.1f}% ({m_cov_br}/{m_br}) "
            f"{'PASS' if ok else 'FAIL':>6}"
        )

    if failures:
        print("\nCOVERAGE FLOOR FAILURES:", file=sys.stderr)
        for msg in failures:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    print(
        "\nAll coverage floors satisfied (overall lines >= 80%, all 10 core modules branch >= 90%)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
