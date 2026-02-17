.PHONY: test test-integration lint

# Run tests per-directory to avoid conftest ImportPathMismatchError when
# multiple tests/conftest.py exist. Coverage is appended and reported at the end.
# Note: --cov-fail-under is not used on the merged run; total coverage is ~73%
# (event_saver, bybit_adapter/rest_client, gridbot/main are low). To enforce
# 80% on one package: uv run pytest <testpath> --cov=<pkg> --cov-fail-under=80
test:
	rm -f .coverage
	uv run pytest packages/gridcore/tests --cov=gridcore -q
	uv run pytest packages/bybit_adapter/tests --cov=bybit_adapter --cov-append -q
	uv run pytest shared/db/tests --cov=grid_db --cov-append -q
	uv run pytest apps/event_saver/tests --cov=event_saver --cov-append -q
	uv run pytest apps/gridbot/tests --cov=gridbot --cov-append -q
	uv run pytest apps/comparator/tests --cov=comparator --cov-append -q
	uv run pytest tests/integration/ --cov-append --cov-report=term-missing -v

# Run cross-package integration tests only
test-integration:
	uv run pytest tests/integration/ -v

# Run ruff linter
lint:
	uv run ruff check .

