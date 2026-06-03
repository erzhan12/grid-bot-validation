# 0065 Code Review — Backtest Wallet Seed Collateral Re-mark

## Findings

### F1 — Mark-feed anchor can apply a ticker mark older than the seed valuation

Severity: P1

The previous first-tick carry-forward issue was addressed by anchoring each collateral coin to `get_mark_at_or_before(symbol, start_ts)`:

- `apps/replay/src/replay/engine.py:119-129`
- `apps/replay/src/replay/engine.py:416-421`

That fixes the case where a valid post-seed, pre-window collateral mark should carry into the first replay tick. However, the feed does not know `seed.at_ts` or the seed mark basis, so it can also pick a ticker mark older than the seed. Example:

- `seed.at_ts = 10:00`
- SOL seed mark came from a fresh wallet `usdValue / balance` at 10:00
- `start_ts = 11:00`
- latest SOLUSDT ticker at-or-before start is 09:00

The feed initializes `_current["SOL"]` to the 09:00 ticker mark, and on the first replay tick `session.update_collateral_mark()` overwrites the 10:00 seed mark with that older ticker value. That creates an immediate false collateral drift even though no post-seed collateral mark was observed.

The plan's seed anchoring is `balance * (mark_t - seed_mark)`, so `mark_t` must not regress behind the seed valuation. The test added for the carry-forward fix covers a pre-window mark, but not the stale-before-seed case:

- `apps/replay/tests/test_collateral_mark_feed.py:89-105`

Suggested fix: pass an anchor floor, probably `config.seed.at_ts`, into `CollateralMarkFeed` and only initialize from `get_mark_at_or_before(symbol, start_ts)` when that row's `exchange_ts >= seed.at_ts`. If only the mark value API is available, add a repository method that returns `(exchange_ts, mark_price)`. Otherwise initialize from the session's seed mark and stream rows from `seed.at_ts` onward. Add a regression test where the only ticker anchor is before `seed.at_ts` and the first tick must keep the seed mark.

### F2 — Collateral switch metadata can misclassify string booleans

Severity: P3

The loader treats `collateralSwitch` and `marginCollateral` by Python truthiness:

- `apps/replay/src/replay/snapshot_loader.py:747-752`

If a recorded payload or fixture contains `"false"` / `"False"` as strings, those values are truthy and the coin will not be recorded in `collateral_switch_off_coins`. The current grounding says Bybit sends booleans, so this is not blocking, but it is still a raw JSON shape edge case.

Suggested fix: normalize bool-like strings for these metadata fields, or log unexpected non-boolean types. Add tests for `False`, missing, and `"false"` if string coercion is supported.

## Resolved Since Prior Review

- Previous F1, first-tick carry-forward missing pre-window marks, was partially fixed by anchoring the mark feed at `start_ts` and adding `test_anchors_carry_forward_pre_window_mark`.
- Previous F2, negative collateral wallet balances being modelled, was fixed by changing the inclusion guard to `wallet_balance <= 0` and adding `test_negative_balance_excluded`.

## Coverage Notes

The implementation has strong focused coverage for the main feature paths: loader inclusion and stale `usdValue` fallback, session equity behavior, recorder public subscriptions, reporter rows, repository helpers, mark-feed carry-forward, and full replay #3a/#3b/#4 integration.

The remaining coverage gap is the anchor-floor scenario above: a pre-`seed.at_ts` ticker should not replace a seed mark derived from wallet valuation.

## Verification

Targeted tests run:

```bash
uv run pytest -q apps/replay/tests/test_snapshot_loader.py apps/replay/tests/test_engine_collateral_integration.py apps/replay/tests/test_collateral_mark_feed.py apps/backtest/tests/test_session.py apps/comparator/tests/test_reporter.py apps/recorder/tests/test_config.py apps/recorder/tests/test_recorder.py shared/db/tests/test_repositories.py
```

Result: `248 passed, 2 skipped`.
