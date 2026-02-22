# 0010 Review: PnL Checker (Post-Fix Re-Review)

## Findings

No actionable code findings were identified in the current implementation.

The previously reported funding comparison mismatch is now resolved at the spec level: `docs/features/0010_PLAN.md` explicitly defines cumulative funding and funding snapshot as informational-only fields, which matches current comparator behavior.

## Residual Risks / Testing Gaps

- `main.py` and `reporter.py` are covered by tests but still have lower coverage than calculation/comparison modules (see coverage summary below).
- This remains a read-only live-data tool; end-to-end behavior against real Bybit responses (timing drift, account-specific edge cases) is only partially represented by unit mocks.

## Validation Performed

- `uv run pytest apps/pnl_checker/tests -q` -> **61 passed**
- `uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -q` -> **91% total coverage**
- `uv run ruff check apps/pnl_checker/src apps/pnl_checker/tests` -> **all checks passed**
- `uv run pytest packages/bybit_adapter/tests/test_rest_client.py -q` -> **56 passed**
