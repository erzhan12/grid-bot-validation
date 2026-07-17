.PHONY: test test-integration lint clear-log live-check

# Run tests per-directory to avoid conftest ImportPathMismatchError when
# multiple tests/conftest.py exist. Coverage gates (issue #214): total >= 88
# via the trailing `coverage report --fail-under=88` step (real total 91% as of
# 2026-07-17); gridcore >= 80 via --cov-fail-under=80 on its invocation, which
# must stay first and non-append (fresh data file scoped by --cov=gridcore).
# The integration run's --cov-append/--cov-report flags are inert (no --cov=
# source, pytest-cov not registered) — the trailing step is the gate site.
test:
	rm -f .coverage .coverage.*
	uv run pytest packages/gridcore/tests --cov=gridcore --cov-fail-under=80 -q
	uv run pytest packages/bybit_adapter/tests --cov=bybit_adapter --cov-append -q
	uv run pytest shared/db/tests --cov=grid_db --cov-append -q
	uv run pytest apps/event_saver/tests --cov=event_saver --cov-append -q
	uv run pytest apps/gridbot/tests --cov=gridbot --cov-append -q
	uv run pytest apps/comparator/tests --cov=comparator --cov-append -q
	uv run pytest apps/recorder/tests --cov=recorder --cov-append -q
	uv run pytest apps/replay/tests --cov=replay --cov-append -q
	uv run pytest apps/backtest/tests --cov=backtest --cov-append -q
	uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-append -q
	uv run pytest apps/live_check/tests --cov=live_check --cov-append -q
	uv run pytest tests/integration/ --cov-append --cov-report=term-missing -v
	uv run coverage report --fail-under=88

# Run cross-package integration tests only
test-integration:
	uv run pytest tests/integration/ -v

# Run ruff linter. check_tier_drift.py is passed explicitly: it is an
# operational script (weekly risk-tier-monitor CI) inside the otherwise
# ruff-excluded scripts/ dir (issue #215).
lint:
	uv run ruff check . scripts/check_tier_drift.py

# Truncate /tmp/gridbot.log before a fresh run
clear-log:
	: > /tmp/gridbot.log

# Reconcile replay vs live over the rolling window (feature 0088).
# Read-only against the live recorder DB; exit 0=PASS 1=FAIL 2=SKIP/no-data.
# Defaults live in the yaml (data/ db path + run_id: null auto-discovers the
# latest recording run); override with --run-id to reconcile a historical run.
live-check:
	uv run --package live-check live-check \
		-c apps/live_check/conf/live_check.yaml \
		--once

