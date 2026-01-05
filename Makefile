.PHONY: test lint

# Run all tests with coverage
test:
	uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v

# Run ruff linter
lint:
	uv run ruff check .

