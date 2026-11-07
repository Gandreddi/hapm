.PHONY: up down clean ps logs lint typecheck test check integration coverage e2e portal-check demo verify all

PY = uv run python
LIBS = libs/telemetry/src libs/events/src libs/policy/src libs/core/src libs/memory/src libs/mcp_client/src
LIB_COV = --cov=hapm_telemetry --cov=hapm_events --cov=hapm_policy --cov=hapm_core --cov=hapm_memory --cov=hapm_mcp_client

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

all: check up demo verify
