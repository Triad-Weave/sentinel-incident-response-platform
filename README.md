# Sentinel — AI On-Call Engineer & Shared Platform Observability

Sentinel is a three-layer cloud-native AI incident investigator built on the cohort shared Kubernetes platform:

1. **Target System (`shop` & `sentinel-chaos`)**: Two FastAPI services (`orders`, `payments`) with independent Postgres databases, a constant-rate `loadgen` (5–20 rps, fixed seed), silent in-process `faultkit` middleware, and an isolated `chaos-controller` holding runtime faults and the ground-truth ledger (`chaos` DB).
2. **Shared Platform Observability (PB-09..PB-11, PB-15 — owned by Sentinel lead)**: `platform-telemetry` shared Python library, `platform-eval` shared `eval-result/v1` schema + CLI (`validate`, `compare`, `push`) + Eval Store, OTel Collector/Prometheus/Loki/Alertmanager configs, and 4 zero-hand-editing Grafana dashboards serving **Sentinel, Lumen, and Folio**.
3. **Sentinel Stack (`sentinel`)**: `sentinel-api` (Alertmanager webhook, REST, Postgres `LISTEN/NOTIFY` SSE, Argon2 RBAC), `sentinel-worker` (3-tool triage pack + single tool-use loop with hard limits + post-action verifier + postmortem drafter + watchdog), `sentinel-mcp` (6 strictly read-only templated tools via official `mcp` SDK), `rollback-executor` (`RollbackRequest` PB-07, zero Git creds), `restart-executor` (rolling pod delete in `shop`, FR-AG-08), React + TypeScript + Vite Incident Console, and `sentinelctl` CLI + 10-scenario eval harness.

---

## Quick Start (Fresh Clone)

### 1. Python environment, tests, linting, typecheck & full verification
```bash
make install            # Creates Python 3.12 .venv via uv and installs all packages
make verify             # Runs Layers L1-L5 (lint, mypy, privilege, T01-T40 + cov floor, 3x random order, 12-bug mutation check, console build) -> docs/verification-report.md
make verify-fast        # Quick daily check (P10)
make verify-mutation    # Plants 12 known defects one by one and proves each is killed by its named test
make verify-privilege   # Verifies RBAC allowlist, NetworkPolicy, AdmissionPolicy, image isolation & 2.5 GB RAM cap
make verify-cluster     # Runs all 17 Layer 6 checks on a live local cluster
```

### 2. Incident Console (React + TypeScript + Vite)
```bash
make console-install    # pnpm install in console/
make console-test       # Vitest + React Testing Library
make console-build      # TypeScript check + production Vite bundle
```

### 3. Full Local Stack (Docker Compose)
```bash
cp .env.example .env
# Optional: set UPSTREAM_LLM_API_KEY to use a live provider through the PB-14 gateway shim;
# leave empty to run the gateway shim in zero-cost deterministic mock mode.
make up
```

Endpoints once running:
- **Incident Console**: http://localhost:8080 (`responder@sentinel.local` / `responder-dev-pass` or `viewer@sentinel.local` / `viewer-dev-pass`)
- **Sentinel API**: http://localhost:8000/docs
- **Grafana (4 Cohort Dashboards)**: http://localhost:3000
- **Prometheus**: http://localhost:9090 | **Alertmanager**: http://localhost:9093 | **RabbitMQ UI**: http://localhost:15672

---

## Operator & Evaluation Commands (`sentinelctl` & `platform-eval`)

```bash
# Inject & clear runtime faults without redeploy (FR-CH-01, FR-CH-02, FR-CH-04)
sentinelctl fault inject latency --service payments --intensity 900ms --duration 10m
sentinelctl fault inject error_rate --service orders --intensity 0.25 --duration 10m
sentinelctl fault inject db_exhaustion --service orders --intensity 5 --duration 10m
sentinelctl fault list
sentinelctl fault clear --all

# Ship & restore bad/benign releases via release.yaml (Architecture p.17)
sentinelctl release ship --service orders --variant itemised-tax
sentinelctl release restore --service orders

# One-command end-to-end evaluation across all 10 scenarios (FR-EV-01..04, PB-15)
sentinelctl eval run --scenarios all --repeat 3 --push

# Security audits (NFR-05 & FR-CH-03)
sentinelctl audit secrets
sentinelctl audit leaks

# Shared platform eval CLI for Sentinel, Lumen, and Folio (PB-15)
platform-eval validate eval/reports/<run>.json
platform-eval compare <baseline.json> <candidate.json>
```

---

## Repository Structure

```text
platform/
  telemetry/         # `platform-telemetry` shared Python package (PB-09, PB-11)
  eval/              # `platform-eval` schema, CLI (validate/compare/push), Eval Store (PB-15)
  observability/     # OTel Collector, Prometheus, Loki, Alertmanager, 4 Grafana dashboards
  dev-shims/         # Local dev shims for Postgres 4-DB init, PB-14 LLM gateway, PB-07 rollback controller
services/
  faultkit/          # Silent in-process fault middleware (latency, error_rate, db_exhaustion, memory_leak, cpu_saturation)
  orders/            # Shop Orders FastAPI service + VERSION
  payments/          # Shop Payments FastAPI service + VERSION + NFR-05 canary
  loadgen/           # Constant-arrival-rate load generator (5-20 rps, seed 42)
chaos/controller/    # Isolated ground-truth controller & ledger (namespace: sentinel-chaos)
sentinel/
  common/            # SQLAlchemy models, Alembic migrations, queue adapter, redactor, scanners, schemas
  api/               # Alertmanager intake, REST API, SSE broker, Argon2 auth
  mcp_server/        # Official MCP SDK Streamable HTTP server with 6 read-only templated tools
  worker/            # Triage pack, agent loop, verifier, postmortem drafter, watchdog
  rollback_executor/ # Narrow PB-07 RollbackRequest executor (+ ADR-003 Git PR fallback)
  restart_executor/  # Narrow rolling pod-delete executor (FR-AG-08)
console/             # React + TypeScript + Vite + TanStack Query + EventSource SPA
cli/sentinelctl/     # Operator CLI + deterministic eval scorer
releases/            # Bad-release catalog & 1-commit branch patches
eval/                # 10 scenario YAML files (S01-S10), versioned LLM judge prompt, reports
sentinel-gitops/     # Thin PB-04 charts, Argo CD apps, release.yaml, RBAC, NetworkPolicy, AdmissionPolicies, Alerts
tests/               # Unit tests, E2E demo flow, privilege verifier, Playwright spec
docs/                # Design proposals, 12 Sentinel ADRs + 6 Observability ADRs, improvements, AI log, demo script
```

---

## Key Design & Documentation

- [docs/design-proposal.md](docs/design-proposal.md) — Sentinel Week 1 Design Proposal & answers to all 8 open questions
- [docs/observability-design.md](docs/observability-design.md) — Shared Platform Observability contract for Sentinel, Lumen, and Folio
- [docs/verification-plan.md](docs/verification-plan.md) — 8-layer verification plan, 10 CI proofs, T01–T40, 12 mutations, 17 cluster checks & prompts P1–P10
- [docs/verification-report.md](docs/verification-report.md) — Latest generated verification evidence report (`make verify`)
- [docs/adr/README.md](docs/adr/README.md) — ADR-001..ADR-012 & OBS-001..OBS-006
- [docs/improvements.md](docs/improvements.md) — Eval-driven iteration log
- [docs/ai-log.md](docs/ai-log.md) — Weekly AI-assisted development log
- [docs/demo-script.md](docs/demo-script.md) — 20-minute final demo runbook
