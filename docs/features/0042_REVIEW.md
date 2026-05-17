# Feature 0042 — Code Review (rev 2)

## Summary

All three Important findings from rev 1 are addressed (two by code fix, one
by explicit doc/test). The WS writer now isolates account-field parsing in
its own `try/except` so a malformed account header no longer drops coin
rows. RULES.md and a new short-direction liq test cover the
`current_balance` semantics shift. Recommend approve.

## Status of prior findings

### Important

1. **WS writer try/except scope** — RESOLVED.
   `apps/event_saver/src/event_saver/writers/wallet_writer.py:223-241` now
   wraps the five `decimal_or_zero(...)` account parses in their own
   `try/except`; on failure the account fields fall back to `None` and the
   coin loop at 245-270 still runs and appends every coin row. Matches the
   recorder shape at `apps/recorder/src/recorder/recorder.py:352-368`.
   Test `test_malformed_account_fields_keep_coin_rows`
   (`apps/event_saver/tests/test_writers.py:768-823`) exercises a
   `"not-a-decimal"` `totalAvailableBalance` and asserts both coin rows
   land in the buffer with `total_*` fields set to `None`, and that
   `raw_json["_account"]` still preserves the bad value.

2. **`BacktestSession.current_balance` semantics shift** — RESOLVED (via
   docs + a small targeted test). `RULES.md:2007` now spells out the
   downstream consumers ("order margin gating, wallet-fraction qty
   sizing, margin-ratio logging, and risk multiplier state all see the
   UTA account-level available-balance baseline").
   `apps/backtest/src/backtest/session.py:84-110` carries the same note
   in the class docstring; `apps/backtest/src/backtest/runner.py:697-701`
   adds a one-line note on `_estimate_liquidation_price` that
   `available = wallet_balance` is UTA `totalAvailableBalance` in
   0042-seeded replay. New test
   `test_estimate_liq_price_short_uses_available_balance_semantics`
   (`apps/backtest/tests/test_runner.py:325-340`) asserts the linear
   delta `(uta_available - low_available) / qty` between two liq prices,
   pinning the formula's sensitivity to the new baseline. Not a full
   risk-multiplier smoke test, but the user-chosen mitigation (option b)
   is sufficient given the docstring + RULES coverage.

3. **`WalletSeed` asymmetric defaults** — UNCHANGED.
   `apps/replay/src/replay/snapshot_loader.py:392-407` still zero-coerces
   `total_equity`, `total_margin_balance`, `account_im_rate`,
   `account_mm_rate` when NULL while gating to `None` on
   `total_available_balance IS NULL` (line 389). The docstring at
   105-114 doesn't call out the asymmetry. Low-impact today because
   only `total_available_balance` is consumed; flagged again for 0043
   if hedge credit or IM/MM rate consumers land.

### Minor / nits

1. `timedelta` re-import deletion in `shared/db/tests/test_repositories.py`
   — VERIFIED HARMLESS. Module-level `from datetime import datetime,
   timedelta, UTC` at line 3 covers all remaining usages
   (`grep -n timedelta` shows 426/506/1115/1132/1140 etc. all use the
   module import). The deleted line was a redundant local re-import.

2. Engine log line conflating seed vs config fallback —
   `apps/replay/src/replay/engine.py:238-250` — PARTIAL. The format
   string was split into `coin_balance=%s, total_available_balance=%s`
   and `coin_balance` now correctly logs `None` when `wallet_seed is
   None`. However the `total_available_balance=%s` arg still binds to
   `initial_balance`, so when seed is None the line prints
   `total_available_balance=<config value>` (e.g.
   `total_available_balance=10000.0`) which still conflates the two
   sources. A `source=config|seed` field would close this. Not a
   blocker — the `coin_balance=None` half is already a strong tell.

3. Duplicate account-key constants — UNCHANGED.
   `_ACCOUNT_RAW_JSON_KEYS` at `wallet_writer.py:17-25` and
   `_WALLET_ACCOUNT_RAW_JSON_KEYS` at `recorder.py:57-65` are still two
   identical seven-string tuples in two modules. Rename drift remains a
   real risk; co-locating in `grid_db/_decimal.py` (or a new
   `grid_db/_wallet.py`) is still cheap.

4. Recorder `availableBalance` fallback comment —
   `apps/recorder/src/recorder/recorder.py:373-375` — UNCHANGED. New
   `coin_available in (None, "") and "availableBalance" in coin_data`
   path still lacks a one-liner explaining why a `""` value falls
   through to `decimal_or_zero("")` rather than the `availableBalance`
   key. Low priority.

5. NULL-fallback test reconstructs ternary inline —
   `apps/replay/tests/test_engine_seed.py:323-352` — UNCHANGED. New
   test `test_null_0042_wallet_fields_fall_back_to_config_balance`
   asserts `wallet_seed is None` correctly but then re-implements the
   `wallet_seed.total_available_balance if … else config.initial_balance`
   ternary at 342-346, bypassing `engine.run()`. The actual ternary at
   `engine.py:220-224` is only exercised by the happy-path integration
   test. Could be tightened but acceptable for unit speed.

### Test gaps that were called out

| Gap | Status |
|---|---|
| WS writer drops only offending row on `"NaN"`-style account fields | FILLED — `test_malformed_account_fields_keep_coin_rows` (`test_writers.py:768-823`) |
| Risk multipliers / margin ratios under new balance semantics | PARTIAL — `test_estimate_liq_price_short_uses_available_balance_semantics` (`test_runner.py:325-340`) pins liq formula sensitivity. No full risk-multiplier smoke test; mitigated via RULES.md doc. |
| `load_wallet_seed_full` "no row" branch | NOT FILLED — `TestLoadWalletSeedFull` (`test_snapshot_loader.py:451-511`) only covers happy-path and NULL `total_available_balance`. The branch where `repo.get_latest_before` returns `None` is unexercised. The check `if snap is None or snap.total_available_balance is None` at `snapshot_loader.py:389` has only the second clause covered. Easy to add. |
| Migration script unit test | NOT FILLED — consistent with 0034; not a regression. |

## New findings (if any)

None introduced by the fixes.

Small observations:

- `wallet_writer.py:237-241` sets the five account fields to `None` on
  parse failure. Model columns are `Numeric(20,8) NULL` (see
  `shared/db/src/grid_db/models.py:440-453`), so `None` is a legal value
  that lands as SQL NULL. `WalletSeed`'s gate at
  `snapshot_loader.py:389` then treats this row exactly like a
  legacy/migrated row (returns `None` from `load_wallet_seed_full`,
  replay falls back to config). That's the right end-to-end behaviour
  — explicitly flagging it so it's not lost in a future refactor.

- `decimal_or_zero` at `shared/db/src/grid_db/_decimal.py:6-15` raises
  `decimal.InvalidOperation` (not `ValueError`) on truly malformed
  strings. The writer's `except Exception` (line 233) catches that;
  the recorder's `except Exception` (line 367) catches that too. No
  silent swallowing — both paths log a `warning` and either fall back
  to `None` or `continue`. Consistent.

## Tests

| Test target | Path | Status |
|---|---|---|
| Repository round-trip with 5 new cols | `shared/db/tests/test_repositories.py:1032-1057` | OK |
| WS writer stamps account fields on every coin | `apps/event_saver/tests/test_writers.py:702-741` | OK |
| WS writer empty-string → `Decimal('0')` | `apps/event_saver/tests/test_writers.py:745-766` | OK |
| WS writer malformed account → None on rows, coins survive | `apps/event_saver/tests/test_writers.py:768-823` | OK (new this rev) |
| REST initial snapshot stamps account + `_account` raw_json | `apps/recorder/tests/test_initial_rest_snapshot.py:139-198` | OK |
| `load_wallet_seed_full` happy path | `apps/replay/tests/test_snapshot_loader.py:451-485` | OK |
| `load_wallet_seed_full` NULL → None | `apps/replay/tests/test_snapshot_loader.py:487-511` | OK |
| `load_wallet_seed_full` no-row → None | (missing) | gap |
| Engine seeds session from `total_available_balance` | `apps/replay/tests/test_engine_seed.py:245-288` | OK |
| Engine NULL fallback to config `initial_balance` | `apps/replay/tests/test_engine_seed.py:323-352` | OK (ternary-inline, see Minor #5) |
| Runner liq scenario sensitive to UTA balance | `apps/backtest/tests/test_runner.py:325-340` | OK (new this rev, narrow but correct) |

Remaining gaps:

- `load_wallet_seed_full` "no row" branch (see table).
- No full risk-multiplier smoke test on a seeded replay; documented via
  RULES.md instead.
- Migration script untested (matches 0034 baseline; not a regression).

## Verdict

**Approve.**

All three Important findings from rev 1 are either fixed in code (WS writer
scope) or addressed by an explicit doc + targeted test (current_balance
semantics). The remaining gaps — `WalletSeed` asymmetric defaults, log line
ambiguity when seed is None, duplicate `_ACCOUNT_RAW_JSON_KEYS`, missing
"no row" branch test for `load_wallet_seed_full` — are all low-impact and
can be picked up in 0043 or left as nits. No blockers.
