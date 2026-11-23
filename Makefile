.PHONY: up down clean ps logs lint typecheck test check integration coverage e2e portal-check demo verify all \
	dev-demo dev-verify config-check pilot pilot-report sentinel-demo sentinel-verify sentinel-engines

PY = uv run python
LIBS = libs/telemetry/src libs/events/src libs/policy/src libs/core/src libs/memory/src libs/mcp_client/src \
	libs/config/src libs/codeintel/src libs/sentinel/src
LIB_COV = --cov=hapm_telemetry --cov=hapm_events --cov=hapm_policy --cov=hapm_core --cov=hapm_memory --cov=hapm_mcp_client \
	--cov=hapm_config --cov=hapm_codeintel --cov=hapm_sentinel

up:
	$(PY) scripts/up.py
down:
	$(PY) scripts/compose.py down
clean:
	$(PY) scripts/compose.py down -v
ps:
	$(PY) scripts/compose.py ps
logs:
	$(PY) scripts/compose.py logs --tail=100

lint:
	uv run ruff check .
	uv run ruff format --check .
typecheck:
	uv run mypy --strict $(LIBS)
test:
	uv run pytest tests/unit
check: lint typecheck test

# Needs the stack (make up). Integration tests count toward the libs coverage floor.
integration:
	uv run pytest tests/integration
coverage:
	uv run pytest tests/unit tests/integration $(LIB_COV) --cov-report=term-missing --cov-fail-under=80

portal-check:
	cd apps/portal && pnpm install --frozen-lockfile --ignore-workspace && pnpm typecheck && pnpm lint
e2e:
	cd apps/portal && pnpm exec playwright test

demo:
	$(PY) -m scripts.demo
verify:
	$(PY) -m scripts.verify

# Developer agent (Phase 1). Offline: fixture repo, mock Jira and GitHub, Fake model, real sandbox.
dev-demo:
	$(PY) -m scripts.dev_demo
dev-verify:
	$(PY) -m scripts.dev_verify
config-check:
	uv run hapm config check
# Office laptop only: needs real config/*.yaml values (refuses otherwise).
pilot:
	$(PY) -m scripts.pilot
pilot-report:
	$(PY) -m scripts.pilot report

# Sentinel context agent (Phase 2). Offline: fixtures, recorded engine outputs, Fake provider.
sentinel-engines:
	docker build -t hapm-sentinel-graphify:0.9.115 -f deploy/sentinel/graphify.Dockerfile deploy/sentinel
	docker build -t hapm-sentinel-scip:0.5.2 -f deploy/sentinel/scip.Dockerfile deploy/sentinel
sentinel-demo:
	$(PY) -m scripts.sentinel_demo
sentinel-verify:
	uv run sentinel build --force
	uv run sentinel verify

all: check up demo verify dev-demo dev-verify sentinel-verify
