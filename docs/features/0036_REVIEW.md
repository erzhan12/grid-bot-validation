# 0036 Review - Wallet writer empty-string balances

## Summary

No blocking findings.

The implementation matches `docs/features/0036_PLAN.md`:

- Adds private `_decimal_or_zero(value)` in `wallet_writer.py`.
- Maps only `None` and `""` to `Decimal("0")`.
- Leaves normal numeric strings, including `"0"`, on the direct `Decimal(str(value))` path.
- Lets malformed non-empty values raise and flow through the existing wallet parse warning path.
- Uses the helper for both `walletBalance` and `availableToWithdraw`.
- Keeps `WalletSnapshot` schema behavior unchanged and preserves original payload values in `raw_json`.
- Adds the requested `RULES.md` pitfall entry.

## Findings

None.

## Review Notes

- No data-shape alignment issue was introduced. The writer still reads the existing Bybit shape `message["data"][].coin[]` and the changed fields remain camelCase (`walletBalance`, `availableToWithdraw`) as expected by current tests and production parsing.
- The change is appropriately scoped. It does not audit or alter the other writers called out for feature 0037, and it does not move parsing into a shared module prematurely.
- The known broad `try/except` blast radius remains unchanged and is pinned by a test: coins before a malformed coin survive, while the malformed coin and later coins in the same wallet update are dropped.
- Style is consistent enough for this codebase. The helper is small, module-private, and close to its call sites.

## Test Review

Coverage added for the new behavior:

- Helper maps `None` and `""` to zero.
- Helper parses `"1.5"` and `"0"` as normal Decimal values.
- Helper raises `decimal.InvalidOperation` for `"not-a-number"`.
- Writer accepts empty-string balances without emitting `Error parsing wallet snapshot`.
- Writer preserves missing-key fallback behavior.
- Writer drops and warns for malformed non-empty balance values.
- Writer pins the current mixed-payload blast radius.

The tests are isolated, fast, and follow the existing `TestWalletWriter` async style with `mock_db` and direct buffer assertions.

## Verification

```bash
uv run pytest apps/event_saver/tests/test_writers.py -k TestWalletWriter -q
# 9 passed, 34 deselected in 0.24s
```

```bash
uv run pytest apps/event_saver/ -q
# 149 passed in 1.73s
```

```bash
uv run ruff check apps/event_saver/src/event_saver/writers/wallet_writer.py apps/event_saver/tests/test_writers.py
# All checks passed!
```

The broader plan lint command currently fails on unrelated pre-existing lint issues outside this feature's changed files:

```bash
uv run ruff check apps/event_saver/
```

- `apps/event_saver/src/event_saver/collectors/public_collector.py`: unused `UTC`
- `apps/event_saver/src/event_saver/main.py`: f-string without placeholders
- `apps/event_saver/tests/test_config.py`: unused `pytest`
- `apps/event_saver/tests/test_reconciler.py`: unused `patch`
