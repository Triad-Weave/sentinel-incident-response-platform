.PHONY: install static test test-random integration contract charts verify-ci e2e verify-privilege verify-privilege-static leak-test verify verify-fast verify-mutation verify-cluster lint typecheck console-install console-test console-build up down eval audit-secrets

VENV := .venv
PYTHON := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy
PIP_AUDIT := $(VENV)/bin/pip-audit
MUTMUT := $(VENV)/bin/mutmut
REPORT_PATH ?= docs/verification-report.md

MYPY_TARGETS := \
	platform/telemetry/platform_telemetry \
	platform/eval/platform_eval \
	services/faultkit/faultkit \
	services/orders/orders_app \
	services/payments/payments_app \
	services/loadgen/loadgen_app \
	chaos/controller/chaos_controller \
	sentinel/common \
	sentinel/api/sentinel_api \
	sentinel/mcp_server/sentinel_mcp \
	sentinel/worker/sentinel_worker \
	sentinel/rollback_executor/rollback_executor \
	sentinel/restart_executor/restart_executor \
	cli/sentinelctl/sentinelctl

DOCKERFILES := \
	services/orders/Dockerfile \
	services/payments/Dockerfile \
	services/loadgen/Dockerfile \
	chaos/controller/Dockerfile \
	sentinel/api/Dockerfile \
	sentinel/worker/Dockerfile \
	sentinel/mcp_server/Dockerfile \
	sentinel/rollback_executor/Dockerfile \
	sentinel/restart_executor/Dockerfile \
	console/Dockerfile

K8S_MANIFESTS := \
	sentinel-gitops/policies/namespaces.yaml \
	sentinel-gitops/policies/rbac.yaml \
	sentinel-gitops/policies/network-policies.yaml \
	sentinel-gitops/policies/admission-policies.yaml \
	sentinel-gitops/policies/rollback-crd.yaml \
	sentinel-gitops/alerts/sentinel-alerts.yaml \
	sentinel-gitops/apps/argocd/applications.yaml

install:
	uv sync --extra dev

# Layer 1: Static checks (all real tools from docs/verification-plan.md)
static:
	uv lock --check
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) $(MYPY_TARGETS)
	pnpm --dir console lint
	pnpm --dir console typecheck
	hadolint $(DOCKERFILES)
	yamllint -c .yamllint.yaml eval/ sentinel-gitops/
	actionlint
	gitleaks detect --source . --no-banner
	$(PIP_AUDIT) --skip-editable
	pnpm --dir console audit --prod --audit-level high

lint:
	$(RUFF) check .
	$(RUFF) format --check .

typecheck:
	$(MYPY) $(MYPY_TARGETS)
	pnpm --dir console typecheck

# Layer 2: Unit tests with >=80% overall line coverage AND >=90% branch coverage on all 10 core modules + Console Vitest (T35)
test:
	$(PYTEST) -m "not integration and not contract and not needs_cluster" -q \
		--cov=sentinel --cov=services --cov=chaos --cov=cli \
		--cov=platform/eval/platform_eval --cov=platform/telemetry/platform_telemetry \
		--cov-branch --cov-fail-under=80 \
		--cov-report=term-missing:skip-covered \
		--cov-report=json:coverage.json \
		--cov-report=xml:coverage.xml \
		--junitxml=junit-unit.xml
	$(PYTHON) scripts/check_coverage_floors.py coverage.json
	pnpm --dir console test

test-random:
	$(PYTEST) -m "not integration and not needs_cluster" -q --no-cov --randomly-seed=101
	$(PYTEST) -m "not integration and not needs_cluster" -q --no-cov --randomly-seed=202
	$(PYTEST) -m "not integration and not needs_cluster" -q --no-cov --randomly-seed=303

# Layer 3: Integration tests against real Postgres via testcontainers (postgres:16-alpine)
integration:
	$(PYTEST) -m integration -q --junitxml=junit-integration.xml

# Layer 4: Contract tests (T29, T32, T33) + PB-15 eval-result/v1 validation
contract:
	$(PYTEST) -m contract -q --junitxml=junit-contract.xml
	@for f in eval/reports/*.json; do \
		if [ -f "$$f" ]; then \
			$(VENV)/bin/platform-eval validate "$$f" || exit 1; \
		fi; \
	done

# Layer 5: Helm lint + kubeconform on standalone K8s manifests + helm template|kubeconform when service-lib chart is available
charts:
	@for c in sentinel-gitops/charts/*; do \
		helm lint "$$c" || exit 1; \
		if ls "$$c"/charts/service-lib*.tgz >/dev/null 2>&1; then \
			helm template test "$$c" | kubeconform -strict -ignore-missing-schemas -summary || exit 1; \
		fi; \
	done
	kubeconform -strict -ignore-missing-schemas -summary $(K8S_MANIFESTS)

verify-ci:
	$(PYTHON) scripts/verify.py --ci --report "$(REPORT_PATH)"

verify:
	$(PYTHON) scripts/verify.py --report "$(REPORT_PATH)"

verify-fast:
	$(PYTHON) scripts/verify.py --fast

verify-mutation:
	rm -rf mutants
	$(MUTMUT) run --max-children 2
	$(MUTMUT) results

# Layer 6: Live Kubernetes cluster checks (require running cluster)
e2e:
	SENTINEL_LIVE_CLUSTER=1 $(PYTEST) -m needs_cluster -v

verify-privilege:
	$(PYTHON) tests/e2e/verify_privilege.py --live

verify-privilege-static:
	$(PYTHON) tests/e2e/verify_privilege.py --offline

leak-test:
	$(VENV)/bin/sentinelctl audit leaks

verify-cluster:
	bash scripts/verify_cluster.sh

console-install:
	cd console && pnpm install --frozen-lockfile

console-test:
	cd console && pnpm test

console-build:
	cd console && pnpm build

up:
	docker compose up --build -d

down:
	docker compose down -v

eval:
	$(VENV)/bin/sentinelctl eval run --scenarios all

audit-secrets:
	$(VENV)/bin/sentinelctl audit secrets
