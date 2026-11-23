# Sentinel — Verification Report

- **Generated (UTC):** 2026-11-22 14:51:15 UTC
- **Sentinel commit:** `dc6dc9d` (uncommitted working tree changes present)
- **Execution environment:** `local` (CI (`make verify-ci`))
- **Runnable automated checks verdict:** **PASS (with explicit NOT RUN items below)**
- **Layers 6–8 (Cluster, Evaluation, Acceptance):** **NOT RUN** — no Kubernetes cluster or shared platform exists yet.

## Layer-by-Layer Results

| Layer | Check | Where | Kind | Result | Time (s) | Evidence |
|---|---|---|---|---|---:|---|
| L1 Static | uv lock --check | local | static | **PASS** | 0.09 | uv.lock up to date |
| L1 Static | ruff check . | local | static | **PASS** | 0.05 | 0 issues |
| L1 Static | ruff format --check . | local | static | **PASS** | 0.01 | all files formatted |
| L1 Static | mypy (14 packages) | local | static | **PASS** | 0.71 | Success: no issues found in 77 source files |
| L1 Static | console eslint (`pnpm --dir console lint`) | local | static | **PASS** | 1.25 | 0 eslint errors |
| L1 Static | console TypeScript (`tsc --noEmit`) | local | static | **PASS** | 0.86 | 0 type errors |
| L1 Static | hadolint (all 10 Dockerfiles) | local | static | **PASS** | 0.11 | 10 Dockerfiles clean |
| L1 Static | yamllint (eval/ & sentinel-gitops/) | local | static | **PASS** | 0.16 | 0 yaml lint errors |
| L1 Static | actionlint (.github/workflows/) | local | static | **PASS** | 0.10 | 0 workflow issues |
| L1 Static | gitleaks (full git history) | local | static | **PASS** | 0.07 | 0 leaks in git history |
| L1 Static | pip-audit (--skip-editable) | local | static | **PASS** | 0.47 | 0 known Python CVEs |
| L1 Static | pnpm audit --prod --audit-level high | local | static | **PASS** | 5.24 | 0 high/critical JS CVEs |
| L1 Static (extra) | Offline YAML/Dockerfile manifest lint (NOT cluster privilege/network/memory proof) | local | static | **STATIC-EXTRA-OK** | 0.05 | static YAML/Dockerfile invariants checked (live cluster enforcement still NOT RUN) |
| L2 Unit | pytest unit suite (T01–T11, T22, T24–T26, T28, T30, T34) + >=80% floor | local | static | **PASS** | 7.22 | 65 passed, 19 deselected in 6.78s; TOTAL 4759 749 1064 165 82% |
| L2 Unit | Coverage floors (>=80% overall lines, >=90% branch on 10 core modules) | local | static | **PASS** | 0.02 | Overall line coverage: 84.26% (4010/4759) [floor >= 80.0%] \| Overall branch coverage: 69.83% (743/1064) |
| L2 Unit | Console Vitest (T35: statuses, SSE timeline, reason, viewer RBAC) | local | static | **PASS** | 1.23 | Vitest passed |
| L2 Unit | 3x randomized-order anti-flake check (pytest-randomly) | local | static | **PASS** | 9.65 | seed=101: PASS (68 passed, 16 deselected in 2.67s); seed=202: PASS (68 passed, 16 deselected in 2.76s); seed=303: PASS (68 passed, 16 deselected in 3.10s) |
| L3 Integration | pytest -m integration (real testcontainers postgres:16-alpine: T12–T21, T23, T27, T31) | local | static | **NOT RUN** | 0.00 | Docker daemon not running locally (runs in CI on ubuntu-latest with postgres:16-alpine) |
| L4 Contract | pytest -m contract (T29 eval-result/v1, T32 tool/finding snapshots, T33 Alertmanager v4) | local | static | **PASS** | 0.92 | 3 passed, 81 deselected in 0.61s |
| L4 Contract | Console Vite production build (`pnpm --dir console build`) | local | static | **PASS** | 1.50 | built cleanly |
| L5 Charts & Images | helm lint (10 charts under sentinel-gitops/charts/) | local | static | **PASS** | 0.36 | 10 charts linted, 0 failed |
| L5 Charts & Images | kubeconform -strict (7 standalone K8s manifest files: policies, alerts, Argo apps) | local | static | **PASS** | 3.70 | 31 core K8s resources valid, 0 invalid (6 CRDs skipped) |
| L5 Charts & Images | helm template \| kubeconform -strict (10 thin service-lib charts) | local | static | **NOT RUN** | 0.00 | blocked on unpublished platform OCI dependency oci://ghcr.io/cohort-platform/charts/service-lib (10 charts missing subchart) |
| L5 Charts & Images | Trivy container image CVE scan (CRITICAL/HIGH) | CI | static | **NOT RUN** | 0.00 | runs in CI docker-build-only job (aquasecurity/trivy-action) after Docker build |
| L6 Cluster | Live E2E suite (T36–T40, make e2e) | local | cluster | **NOT RUN** | 0.00 | needs cluster (no Kubernetes cluster running) |
| L6 Cluster | Live privilege audit (kubectl auth can-i vs allowlists) | local | cluster | **NOT RUN** | 0.00 | needs cluster (offline YAML check is NOT a cluster privilege proof) |
| L6 Cluster | Live network isolation (MCP -> chaos-controller timeout) | local | cluster | **NOT RUN** | 0.00 | needs cluster (offline NetworkPolicy YAML is NOT a CNI proof) |
| L6 Cluster | Live memory cap (<2.5 GB after 30m load, T40) | local | cluster | **NOT RUN** | 0.00 | needs cluster (offline resource limits are NOT runtime RSS proof) |
| L6 Cluster | Live leak scan (make leak-test) | local | cluster | **NOT RUN** | 0.00 | needs cluster |
| L7 Evaluation | Full eval run (sentinelctl eval run --scenarios all --repeat 3) | local | cluster | **NOT RUN** | 0.00 | needs cluster |
| L8 Acceptance | Acceptance suite AP0–AP6, 10 CI PR proofs, fresh clone, demo rehearsal | local | cluster | **NOT RUN** | 0.00 | needs cluster & human review (AP0 held until cluster exists) |

## Honest Scope Notes

- Static YAML/Dockerfile checks (`L1 Static (extra)`) NEVER count as `PASS` for live cluster privilege, network isolation, memory cap (`T40`), or `T36`–`T40`.
- Layer 3 Integration tests (`T12`–`T21`, `T23`, `T27`, `T31`) require real Postgres via `testcontainers` (`postgres:16-alpine`) — never SQLite or mocked DBs.
- Layers 6, 7, and 8 remain **NOT RUN** until a real Kubernetes cluster with the shared platform (Argo CD, PB-05 reusable CI, PB-07 rollback controller, PB-04 `service-lib` chart) is provisioned.
