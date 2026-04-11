# 0016 Review

## Findings

No findings.

## Verification

The updated implementation matches the plan:

- [executor.py](/Users/erzhan/DATA/PROJ/grid-bot-validation/apps/gridbot/src/gridbot/executor.py#L170) no longer sends `order_link_id` to Bybit.
- [runner.py](/Users/erzhan/DATA/PROJ/grid-bot-validation/apps/gridbot/src/gridbot/runner.py#L604) adds the exact-duplicate check to `_is_good_to_place()`.
- [runner.py](/Users/erzhan/DATA/PROJ/grid-bot-validation/apps/gridbot/src/gridbot/runner.py#L717) now tracks injected exchange orders by `orderId`, derives `direction` from `side + reduceOnly`, and no longer requires `orderLinkId`.
- [reconciler.py](/Users/erzhan/DATA/PROJ/grid-bot-validation/apps/gridbot/src/gridbot/reconciler.py#L61) no longer depends on `orderLinkId` during startup reconciliation.

Targeted verification passed:

- `uv run pytest -q apps/gridbot/tests/test_runner.py apps/gridbot/tests/test_executor.py apps/gridbot/tests/test_reconciler.py tests/integration/test_runner_lifecycle.py tests/integration/test_engine_to_executor.py`
- Result: `155 passed`

Broader verification passed:

- `uv run pytest -q apps/gridbot/tests tests/integration/test_runner_lifecycle.py tests/integration/test_engine_to_executor.py`
- Result: `297 passed`

## Residual Note

Startup reconciliation now intentionally assumes all open limit orders for `runner.symbol` belong to that strategy/account path, as documented in [reconciler.py](/Users/erzhan/DATA/PROJ/grid-bot-validation/apps/gridbot/src/gridbot/reconciler.py#L67). That is consistent with the current approach after removing Bybit `orderLinkId`, but if the product ever needs to support manual orders or multiple live strategies sharing the same `(account, symbol)`, a different ownership marker will be needed.
