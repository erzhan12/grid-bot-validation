# Feature 0043 Code Review

Review target: current working-tree implementation of
`docs/features/0043_PLAN.md`.

Command: `commands/code_review.md`.

## Findings

### 1. Short-only safe positions no longer preserve the pre-0043 `liqPrice=0` behavior

Severity: P2

Files:

- `apps/backtest/src/backtest/runner.py:775`
- `apps/backtest/tests/test_runner.py:325`

`_estimate_pair_liq_prices()` applies the raw net-short branch to every
`q_net < 0` state, including short-only states where `long_size == 0`.
That removed the old short-side safe cap from `_estimate_liquidation_price()`:
when wallet/equity is much larger than the short notional, the previous code
returned `0` once the computed short liq exceeded `2 * entry_price`, matching
the documented Bybit-safe regime.

This conflicts with Phase 4 of the plan: the existing 0042 short test was
supposed to remain green for `opposite=zero`, meaning a net-short/short-only
case should still match the old short formula. It also risks failing the
Phase 1/Phase 4 "short-only" parity scenario: a small short with large account
equity can now emit a large positive backtest `liq_price` while live Bybit
reports `0`/NULL.

Suggested fix: keep the raw no-cap net-short behavior only when both legs are
open (`L_size > 0 and S_size > 0`), because that is what the Phase 2 hedge
validation table proved. For short-only (`L_size == 0`), preserve the previous
safe cap. Add a regression test where `long_size=0`, `short_size>0`, and
`total_equity` makes `S_entry + pool / S_size > 2 * S_entry`; expected
`liq_short == 0`.

### 2. Missing `totalAvailableBalance` can still seed `current_balance=0`

Severity: P2

Files:

- `apps/event_saver/src/event_saver/writers/wallet_writer.py:264`
- `apps/replay/src/replay/snapshot_loader.py:408`

The loader now correctly rejects `total_equity is None` and
`total_equity <= 0`, so missing `totalEquity` no longer silently seeds a zero
liq pool. But the same data-shape issue remains for `totalAvailableBalance`.
The writer still uses `decimal_or_zero(wallet_data.get("totalAvailableBalance"))`;
if a future Bybit payload or parser mismatch omits that account-level key while
still including `totalEquity`, the DB row stores `total_available_balance=0`.
`load_wallet_seed_full()` only rejects `None`, so replay seeds:

- `BacktestSession.initial_balance = 0`
- `BacktestSession.current_balance = 0`
- `BacktestSession.initial_equity = <valid totalEquity>`

That leaves the 0043 liq pool looking valid while executor margin gating,
wallet-fraction sizing, margin logs, and risk multipliers consume a zero
available-balance baseline. It is a silent data-alignment failure rather than
a hard fallback to config.

Suggested fix: parse required account fields with a helper that distinguishes
missing keys from numeric zero, or add a `total_available_balance <= 0`
defensive reject in `load_wallet_seed_full()` if zero is not meaningful for
the replay seed. Add a writer/loader regression where `totalEquity` is present
but `totalAvailableBalance` is absent, and assert replay refuses the seed
rather than constructing a zero-balance session.

## Plan Implementation Check

The core hedge formula in `BacktestRunner._estimate_pair_liq_prices()` matches
the Phase 2 derivation for paired hedge states:

- one pair input returns `(liq_long, liq_short)`;
- `total_equity` is used as the pool input;
- `calc_maintenance_margin(L_pv + S_pv, symbol, tiers)` is used for full
  combined-notional MM;
- net-long, net-short, fully hedged, and zero-position branches are present;
- net-long safe results are clamped to `0`;
- the old short `> 2x entry -> 0` cap is intentionally removed for hedged
  net-short states, but the implementation currently removes it for short-only
  states too, which is Finding 1.

The replay/session plumbing is otherwise consistent with the plan:
`WalletSeed.total_equity` is passed into
`BacktestSession(initial_equity=...)`, `total_available_balance` continues to
back `current_balance`, and fill-time `refresh_balances()` keeps post-fill
position state aligned with post-fill balances before risk and snapshot
calculations.

`RULES.md` documents the three non-obvious 0043 choices: pair-shaped
liquidation input, `total_equity` rather than `total_available_balance`, and
full tier-MMR on combined notional rather than summed published per-leg
`positionMM`.

## Tests Review

The feature has focused tests for zero positions, fully hedged positions,
net-long and net-short Phase 2 numeric rows, net-long negative clamp,
net-short raw value above `2x entry`, explicit `total_equity` input, NULL/zero
`total_equity` seed rejection, wallet V5 timestamp resolution, and fill-time
balance refresh.

The important test gaps are the two findings above:

- no short-only safe-cap regression for the old 0042 semantics;
- no missing-`totalAvailableBalance` seed refusal test.

No obvious over-engineering or file-size issue stood out in the touched
implementation. The added `refresh_balances()` helper is small and keeps the
fill-time alignment concern localized.

## Verification

Focused tests:

```
uv run pytest -q apps/backtest/tests/test_runner.py apps/backtest/tests/test_session.py apps/event_saver/tests/test_writers.py apps/replay/tests/test_engine_seed.py apps/replay/tests/test_snapshot_loader.py
```

Result: `173 passed`.

Changed-file lint:

```
uv run ruff check apps/backtest/src/backtest/runner.py apps/backtest/src/backtest/session.py apps/backtest/tests/test_runner.py apps/event_saver/src/event_saver/writers/wallet_writer.py apps/event_saver/tests/test_writers.py apps/replay/src/replay/engine.py apps/replay/src/replay/snapshot_loader.py apps/replay/tests/test_snapshot_loader.py
```

Result: `All checks passed!`.
