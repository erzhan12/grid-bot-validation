## Project Overview

Grid trading bot system with pure strategy engine (gridcore), exchange adapter (bybit_adapter), database layer (grid_db), data capture (event_saver), live bot (gridbot), backtest engine, comparator, recorder, replay engine, and PnL checker.

Successfully extracted pure strategy logic from `bbu2-master` into `packages/gridcore/` with zero exchange dependencies.

### Legacy bbu2 paths intentionally not ported (gridcore scope)

These bbu2 code paths exist for products we do not target (Bybit
inverse contracts: BTCUSD, ETHUSD, etc.). gridcore is intentionally
scoped to Bybit linear USDT-perps. Future audits MUST recognize
these as legacy carve-outs and not re-flag them as divergences:

- **`"b..." amount mode`** — bbu2 `bybit_api_usdt.py:509-518`. The
  "b" prefix means "btc-equivalent" with two branches: `BTCUSD` →
  `btc_amount * price` (inverse), non-BTCUSD → `math.ceil(btc_amount
  / price)` (legacy linear non-USDT). Removed from `gridcore/qty.py`
  in Feature 0028. If a config tries to use `b...` it now raises
  `ValueError: invalid amount string`.
- **`"x" mode currency derivation by symbol`** — bbu2
  `bybit_api_usdt.py:496-501`. bbu2 picks `USDT` if `'USDT' in
  symbol` else `symbol[:3]` (e.g., `BTC` for `BTCUSD`). This handles
  inverse contracts margined in coin. Our `gridcore/qty.py` always
  reads `wallet_balance` as USDT — correct for linear USDT-perps,
  would need a redesign (not a bbu2 port) if USDC or inverse support
  is ever added.
- **`BTCUSD`-specific branches anywhere in bbu2** — inverse
  contract logic. Out of scope.

If you ever consider reintroducing inverse / non-USDT support,
start by re-reading the legacy paths in `bbu_reference/`, not by
re-porting them blindly: bbu2's `'USDT' in symbol` heuristic does
not handle USDC pairs (`BTCPERP`, `BTCUSDC`) correctly either.

## Constraints (do not)

Project-specific "what not to do" — pairs with the universal Constraints in `.claude/rules/code-style.md`.

- **Don't edit `bbu_reference/`** — vendored legacy BBU2 bot our code was ported from (its own `ruff.toml`, line-length 140; outside our lint/test scope). Read it to understand original behavior (see "Legacy bbu2 paths" above), but never modify it.
- **Don't hand-edit generated/data dirs** — `data/`, `output/`, `results/`, `db/` are gitignored recorder/replay/backtest artifacts and SQLite DBs, not source. `conf/` holds real tracked config (risk-limit tiers, instrument caches) — leave unless asked.
- **Backward-compat is deliberate, not speculative** — existing compat (`DirectionType`/`SideType` StrEnum aliases, replay `strict_cross` baseline, `extract_client_order_prefix` no-hyphen fallback) is intentional. Don't add new compat shims without a stated reason.
- **No dead config fields** — don't add YAML fields/flags "for later"; e.g. `max_margin` is declared but never read. (Feature 0079 added a real automatic position cap via the **C1 `safety_caps.max_notional_per_symbol`** notional limit — see "Production safety caps"; `max_margin` itself remains dead/unrevived.)
- **Don't point tooling at live state without explicit ask** — the account is Bybit **mainnet** (`mainnet_live`); never run against the live gridbot DB or live orders unless told.

## Running Tests

```bash
# Run ALL tests (recommended — runs each package separately to avoid conftest conflicts)
make test

# Run per-package
uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v
uv run pytest packages/bybit_adapter/tests -v
uv run pytest shared/db/tests -v
uv run pytest apps/event_saver/tests -v
uv run pytest apps/gridbot/tests -v
uv run pytest apps/backtest/tests -v
uv run pytest apps/comparator/tests --cov=comparator --cov-report=term-missing -v
uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -v
uv run pytest apps/live_check/tests -v

# Integration tests only
make test-integration
```

**`make test` note**: Runs pytest separately per package to avoid `conftest` ImportPathMismatchError. Coverage accumulates across the 11 package runs into one `.coverage`; a trailing `coverage report --fail-under=88` step gates the merged total (real ≈91% as of 2026-07-17). Covers every `pyproject.toml` `testpaths` entry, including `apps/backtest/tests` (added for issue #178 — its prior omission was an oversight with no documented justification).

## Continuous Integration (`.github/workflows/ci.yml`)

The merge gate (feature 0073, issue #176). Two parallel jobs — `test` (`make test`) and `lint` (`make lint`) — run on every PR and on push to `main`; both fail the workflow on any non-zero exit. **CI green is the source of truth for repo health.**

### Dependency / lockfile discipline (Feature 0097, issue #212)

- **Local install:** plain `uv sync` is the day-to-day command (resolves freely).
- **After editing any workspace `pyproject.toml`** (root or member under `packages/`, `shared/`, `apps/`) or dependency metadata: run `uv lock` at repo root and commit the manifest change together with the updated `uv.lock`.
- **CI / scheduled / release workflows:** must use `uv sync --locked` (not plain `uv sync`). Any new workflow that installs deps must follow this — today: `.github/workflows/ci.yml` and `.github/workflows/risk-tier-monitor.yml`.
- **Pre-commit:** `uv lock --check` runs via the local `uv-lock-check` hook in `.pre-commit-config.yaml` when a matching workspace manifest changes. Install hooks once per clone (or after pulling this change): `uv run pre-commit install`. (`bbu_reference/**` is out of scope.)

Phase-0 rollout while the repo is red:
- **Step A (current)** — real failing jobs, no `continue-on-error`; the check is NOT required in branch protection, so it doesn't block the fix PRs for #177–#180.
- **Step A′** — advisory green via `continue-on-error: true` on `lint` ONLY (never `test`); opt-in scaffolding, avoid unless explicitly requested.
- **Step B** — after #177–#180 land and both jobs are green, drop any `continue-on-error` and mark the check required.

**Branch protection (making the check "required") is a GitHub repo setting, not a file** — it cannot be enforced from `ci.yml`; set it manually.

### Lint / Ruff (Feature 0078, issue #181)

`make lint` = `uv run ruff check . scripts/check_tier_drift.py`. Root config lives in `pyproject.toml` `[tool.ruff]` (ruff defaults; only an additive `exclude` list). Excluded trees, each with a rationale comment in the config:
- `apps/backtest/debug_walkthrough.py` — interactive debug walkthrough; not app code.
- `bbu_reference` — vendored bbu2 reference; not maintained, has its own nested `ruff.toml` (line-length 140).
- `scripts` — one-off research/migration tooling; disposable, not imported by apps. Carve-out: `scripts/check_tier_drift.py` is operational and linted via the explicit path in the `make lint` recipe (issue #215 / feature 0091) — an explicit positional file arg overrides the directory exclude (no `--force-exclude` set).

Maintained code is NOT excluded. When a maintained file has an intentional lint error, prefer a targeted `# noqa: <code>` over excluding it — e.g. `tests/integration/conftest.py:16` carries `# noqa: E402` on the `gridcore.config` import that must follow the `sys.path.insert` block.

### Coverage gate (Feature 0092, issue #214)

Two gates in `make test`, both enforced by CI because it runs `make test`:
- **Total ≥ 88** — trailing `uv run coverage report --fail-under=88` step, evaluated on the merged `.coverage` accumulated by the 11 per-package pytest runs (real total ≈91% as of 2026-07-17).
- **gridcore ≥ 80** — `--cov-fail-under=80` on gridcore's own pytest invocation. Valid only because that invocation is scoped by `--cov=gridcore` and writes a fresh data file (first in sequence, no `--cov-append`, right after `rm -f .coverage .coverage.*`). It must stay first AND non-append — moved later without adding `--cov-append`, it erases the accumulated data feeding the total gate.

The integration run's `--cov-append`/`--cov-report` flags are inert (no `--cov=` source, so pytest-cov never registers) — the merged total covers the 11 package runs only. Other packages are intentionally ungated; both thresholds sit below real coverage for churn headroom.

---

## Logging Configuration

Gridcore uses Python's standard library `logging` module. Loggers are named after their modules (`gridcore.grid`, `gridcore.engine`, `gridcore.position`).

### Log Levels
- `INFO` - Important events: grid rebuild, position adjustments
- `DEBUG` - Detailed state info: position calculations

### Logged Events
- **grid.py**: Grid rebuild when price moves out of bounds
- **engine.py**: Grid build from anchor/market price, rebuild due to too many orders
- **position.py**: Position ratio adjustments, risk management triggers

## Testing

### Cross-Package Integration Tests (`tests/integration/`)

```
tests/integration/
├── __init__.py
├── conftest.py                    # Shared fixtures (make_ticker_event, generate_price_series)
├── test_engine_to_executor.py     # 15 tests: GridEngine → IntentExecutor pipeline + REST payload mapping
├── test_backtest_to_comparator.py # 5 tests: BacktestEngine → Comparator round-trip
├── test_runner_lifecycle.py       # 9 tests: StrategyRunner full lifecycle (fills, position, same-order)
├── test_eventsaver_db.py          # 10 tests: EventSaver → Database pipeline + writer integration
└── test_shadow_validation.py      # 6 tests: Shadow-mode dual-path validation
```

**Shadow-Mode Validation Pipeline** (`test_shadow_validation.py`): feeds identical price data through two independently constructed paths — **Path A** is `BacktestEngine` (orchestrated, high-level), **Path B** is manual `GridEngine + BacktestOrderManager` (low-level, mimics shadow mode) — and validates trade count match, deterministic client_order_ids, 100% comparator match rate, zero price/qty deltas, identical PnL totals. Uses `generate_price_series()` for reproducible sine-wave price oscillation.

### Test Pitfalls

1. **Mocking `async def` functions in cli() tests**: When `cli()` calls `asyncio.run(main(...))`, patching `main` with `return_value=0` auto-creates an `AsyncMock` that still returns a coroutine. Use `_close_dangling_coro(mock_run)` helper (in `test_main.py`) to close the unawaited coroutine after assertions, silencing warnings.
2. **`asyncio.get_event_loop()` deprecation in tests**: Use `asyncio.new_event_loop()` instead of `asyncio.get_event_loop()` when setting up event loops in non-async test methods (e.g., `saver._event_loop = asyncio.new_event_loop()`).
3. **Import ordering in test files**: Never place class/dataclass definitions between import blocks. All imports must be grouped at the top of the file before any class or function definitions (e.g., `test_eventsaver_db.py` had `SeededDb` splitting import blocks).
4. **`integration_helpers.py` import path**: `tests/integration/conftest.py` adds `tests/integration/` to `sys.path` explicitly so `import integration_helpers` works even when pytest is invoked without the root `pyproject.toml` `pythonpath` setting (e.g., per-app test runs).
5. **`_fetch_wallet_balance` fallback**: Returns `0.0` when no USDT balance is found in the wallet API response, but now logs `logger.warning` first so unexpected API structures are visible in logs.
6. **generate_price_series**: Uses sine-wave oscillation; period = `num_ticks / 4` (4 complete oscillations). Increase `amplitude` for more fills.
7. **Shadow-Mode Qty Calculator**: Must replicate `BacktestEngine._create_qty_calculator()` exactly, including `InstrumentInfo.round_qty()` ceil rounding.

## Margin Ratio vs Bybit positionIM — Critical Distinction

**`PositionState.margin` = `positionValue / walletBalance`** (a ratio, e.g., 0.26) — bbu2 pattern.

All risk config thresholds (`max_margin=8`, `min_total_margin=0.15`) are ratios. **Bybit's `positionIM`** is a dollar amount — completely different. Do NOT use `positionIM` as `margin`. All consumers (gridbot, pnl_checker, backtest) compute `positionValue / walletBalance`.

---

## Common Pitfalls (Cross-Cutting)

1. **DO NOT** use raw strings for enums — use `GridSideType`, `DirectionType`, `SideType`, `RunType`
2. **ALWAYS** run tests before committing (`make test`)
3. **ALWAYS** use `redact_db_url()` when logging database URLs
4. **ALWAYS** use `asyncio.run_coroutine_threadsafe()` for WS thread → event loop routing
5. **Duplicate orders**: Deterministic `client_order_id` (SHA256) for dedup
6. **Test anchor/grid state**: Verify against actual grid structure, not just input values
7. **conftest conflicts**: Run test suites per-directory (or use `make test`)
8. **SQLite strips timezone**: Use naive UTC timestamps in test data
9. **Blocking I/O in async**: Wrap with `asyncio.to_thread()` (Python 3.9+)
10. **Dict iteration in async**: Snapshot with `list(d.items())` before iterating
11. **`asyncio.CancelledError`**: Is `BaseException`, passes through `except Exception`
12. **Logging style**: Use `%s`-style in hot-path loops; f-strings elsewhere acceptable
13. **PlaceLimitIntent constructor**: Requires `qty` and `grid_level` positional args
14. **`Decimal("")` raises `decimal.InvalidOperation`**: Bybit may send empty strings for unused/dust numeric fields on mainnet UTA (e.g. `walletBalance`, `availableToWithdraw`). `d.get(key, "0")` only handles *missing* keys, not present-but-empty. For non-nullable Decimal columns use a `_decimal_or_zero(value)` helper (predicate `value in (None, "")`, fallback `Decimal("0")`); for nullable columns use the 0034 recorder pattern `Decimal(str(v)) if v not in (None, "") else None`. See `apps/event_saver/src/event_saver/writers/wallet_writer.py:17-34` and `apps/recorder/src/recorder/recorder.py:450-453`. Truthiness checks (`if v:`) are wrong here because they conflate legitimate `"0"` with empty.
15. **Bybit V5 wallet WS frame puts the exchange timestamp at `msg["creationTime"]` (ms, frame top), not inside `data[i]["updateTime"]`**. Pre-V5 / V3 fixtures still set the inner `updateTime`. `wallet_writer._resolve_exchange_ts` tries `updateTime` first, then frame `creationTime`, then `local_ts` as a guard — never falls to epoch. Don't read `wallet_data["updateTime"]` directly on real V5 traffic; it is missing and `int(None or 0) = 0` silently produces 1970-01-01, which then sorts below the single REST snapshot in `WalletSnapshotRepository.get_latest_before` and freezes 0042 seed lookups on the recorder-start balance.
16. **Hedge-mode liquidation (feature 0043) is computed pair-shaped, not per-leg.** **TL;DR:** call `BacktestRunner._estimate_pair_liq_prices(long_state, short_state, total_equity) -> (liq_long, liq_short)`. Pool input is `BacktestSession.total_equity` (NOT `current_balance`). MM is `calc_maintenance_margin(L_pv + S_pv, symbol, tiers)` (full tier-MMR on combined notional, NOT the sum of per-leg `positionMM`). The over-hedged smaller leg is `0` by construction. Three non-obvious choices (derived from 13 mainnet validation snapshots, see `docs/features/0043_PLAN.md` Phase 2): (a) pool input is `BacktestSession.total_equity` (UTA `totalEquity`), **not** `current_balance` / `totalAvailableBalance` — `total_available_balance` undershoots live by 30-45 USDT in net configurations; (b) MM term is `calc_maintenance_margin(L_pv + S_pv, symbol, tiers)` — **full** tier-MMR on the combined notional, not the sum of per-leg `position_mm` from Bybit's WS payload (Bybit publishes the smaller leg's MM with a hedge discount but reverts to full MMR internally for liq calc); (c) `_process_fill` calls `session.refresh_balances(post_fill_unrealized)` so the emitted parity snapshot, risk multipliers, and log all see synchronous post-fill equity instead of the previous tick's value. Don't reintroduce a per-leg `_estimate_liquidation_price` — the pair function is the only liq formula in the codebase.
17. **PositionComparator state-consistency filter (feature 0044).** Pairs matched by exchange_ts but where backtest state has drifted from live (size delta > `state_size_tolerance` default 0.001, or relative entry drift > `state_entry_rel_tolerance` default 0.001 = 0.1%) are flagged `state_diverged=True` on the `PositionComparisonPair`, counted in `ValidationMetrics.position_pairs_state_diverged`, and **excluded** from `liq_price_*` / `position_im_*` / `position_mm_*` / `unrealised_pnl_*` aggregates. They still appear in `position_comparison.csv` (with the `state_diverged` column = `1`) for diagnostic inspection. Why: operator manual fills, missed grid orders, and other state-divergence artefacts otherwise pollute the headline `liq_price_max_abs_delta` metric. Re-validating 0043 on the original noisy DB dropped the metric from 17.77 USDT (dominated by 2 manual-intervention outliers) to 0 USDT. Tolerances are constructor kwargs — relax via `PositionComparator(state_size_tolerance=Decimal("2.0"), ...)` for runs where you want to compare states that drifted by more than a step. See `docs/features/0044_PLAN.md`.

18. **Hedge-aware `positionIM` / `positionMM` on `PositionSnapshot` (feature 0045) — inline pair helper, no per-leg primitives on the emission path.** **TL;DR:** in `BacktestRunner._emit_position_snapshot`, call `self._estimate_pair_im_mm(long_state, short_state, mark_price) -> (im_long, mm_long, im_short, mm_short)` inline and pick the leg matching the snapshot direction. Do **not** reintroduce `calc_initial_margin(L_pv, ...)` / `calc_maintenance_margin(L_pv, ...)` on the snapshot path — those primitives omit Bybit's fee-to-close component (a ~0.23 USDT single-leg gap) AND the hedge cross-credit (a ~1 USDT paired-hedge gap). They stay in `gridcore.pnl` for non-snapshot callers (`pnl_checker` and other pure single-leg sites). Three non-obvious choices, derived from 10 paired LTCUSDT mainnet snapshots + Bybit help-center docs (see `docs/features/0045_PLAN.md` Phase 1 and `docs/features/0045_REVIEW.md`): (a) `positionIM` / `positionMM` returned by Bybit's `/v5/position/list` **include the estimated fee-to-close** — `fee_long = L_size × L_entry × (1 − 1/leverage) × taker_rate`, `fee_short = L_size × L_entry × (1 + 1/leverage) × taker_rate`; (b) the dominant leg's MM uses **only the unhedged portion** at the leg's full-pv tier (`max((L − S) × mark × MMR_tier − deduction_tier, 0)`) — the hedged portion contributes zero to the dominant leg's published MM because Bybit cross-credits it to the smaller leg; the `deduction_tier` term carries through to keep the per-tier MM formula continuous at tier boundaries (matters once any leg crosses tier 1, e.g. LTCUSDT pv ≥ 200k); (c) the smaller (fully hedged) leg has **no `pv × MMR` baseline term** — it publishes `fee_to_close_smaller + MMR_tier × hedged_size × |L_entry − S_entry| × C`, where `C ≈ 5.657` is an empirical Bybit hedge buffer factor whose closed form is not yet documented and needs per-symbol calibration. Config inputs live on `BacktestStrategyConfig.taker_fee_rate` (default 0.00075) and `BacktestStrategyConfig.hedge_smaller_buffer_factor` (default 5.657, LTCUSDT @ 10x). The pre-0045 `calc_initial_margin` / `calc_maintenance_margin` on the snapshot path was wrong even for single-leg cases (missed fee-to-close); the new helper closes the gap for both single-leg and paired-hedge configurations. Single consumer per emit — call inline, no precompute / kwargs threading (contrast with 0043 liq, where two consumers in `_process_fill` justify a precomputed pair).

19. **Non-USDT collateral re-marking on backtest `total_equity` (feature 0065).** When the trader holds non-USDT spot (e.g. SOL) as UTA collateral, live `totalEquity` floats with that coin's mark while a pre-0065 backtest stayed anchored to the seed snapshot — surfacing as `liq_price_*`/`position_im_*`/`position_mm_*` parity drift (those metrics read `BacktestSession.total_equity`, see entry 16). 0065 adds a collateral re-mark **delta** to `total_equity` ONLY. Non-obvious points:
    - **Bybit `totalEquity` excludes the collateral value ratio** — the re-mark term is the FULL asset USD value `Σ balance × mark` (anchored: `+ (collateral_now − Σ balance × seed_mark)`), with **no** ratio/haircut. The ratio applies to *margin balance*, not `totalEquity`. `SeedConfig.collateral_value_ratios` is stored for a FUTURE margin-parity feature and is **not** applied here.
    - **Term moves `total_equity` only.** `current_balance`, `equity_curve`, and `finalize().final_balance` stay available-based (`initial_balance + pnl_delta`) so executor sizing/qty/PnL/fee parity is byte-identical. Applied in BOTH `BacktestSession.update_equity` AND `refresh_balances` (the latter is what `process_fills` calls before reading `total_equity` for the emitted pair-liq snapshot — omitting it there lags the metric one collateral step on fill rows).
    - **Mark feed runs at the TOP of the engine tick loop**, before `process_fills` (`engine.py` `CollateralMarkFeed.mark_at(coin, tick.exchange_ts)` → `session.update_collateral_mark`). The traded-symbol provider does not carry collateral ticks; the feed streams each coin's `*USDT` `ticker_snapshots` with carry-forward at-or-before semantics and a monotonic per-coin cursor (assumes non-decreasing tick ts).
    - **Seed-mark valuation basis (load-bearing):** `load_collateral_seed` uses `usdValue / wallet_balance` ONLY when the per-coin wallet row is FRESH vs `at_ts` (`_strip_tz(at_ts) − _strip_tz(row.exchange_ts) ≤ collateral_wallet_max_staleness`, default 60s); else it falls back to `TickerSnapshotRepository.get_mark_at_or_before(symbol, at_ts)`. Reason: Bybit's UTA wallet WS pushes only CHANGED coins, so a quiet collateral coin carries a stale `usdValue` while `initial_equity` (USDT row) is current — using the stale ratio would inject a false jump on the first tick. Use the module-level `_strip_tz` in `snapshot_loader.py` (engine's is nested in `_seed_pre_check`).
    - **Inclusion gate is the operator list + `wallet_balance > 0`, never the booleans.** `collateralSwitch` / `marginCollateral` in `raw_json` are **booleans** ("usable as collateral"), recorded in `collateral_switch_off_coins` + WARN when off but the coin is STILL re-marked (totalEquity ignores the switch). No row / **non-positive** balance (zero OR negative — a negative/borrowed balance is not spot collateral and would invert the drift) → `collateral_excluded_coins`; balance row but no usable mark → `collateral_missing_mark_coins` (dropped from all model dicts). Returned `coin_balances`/`seed_marks` contain ONLY fully-modelled coins.
    - **`CollateralMarkFeed` anchors each coin's carry-forward to the latest mark in `[seed_at_ts, start_ts]`** in `__init__` (`_anchor_mark`), so the first replay tick uses a correct carry-forward mark when `seed.at_ts < start_ts` or the symbol has no row exactly at `start_ts` (sparse stream). **Floored at `seed_at_ts`** — a mark from BEFORE the seed anchor is NOT applied (it would be false backward drift vs the `at_ts` seed mark); no mark in the window → keep the seed mark. The forward generator then streams rows `>= start_ts`; `mark_at` is forward-only and **raises** on non-monotonic ts.
    - **`seed.collateral_coins` non-empty HARD-REQUIRES a wallet seed** — `engine._load_seed` raises `SeedDataQualityError` (not the soft-fallback to `initial_balance`) when `load_wallet_seed_full` returns None, because the re-mark is anchored to live `total_equity` at `at_ts`. Merge is via `dataclasses.replace` onto the (frozen) `WalletSeed` inside the same DB session.
    - **Recorder:** `RecorderConfig.collateral_symbols` adds the coin's `*USDT` perp to the PUBLIC ticker subscription only (de-duped with `symbols`, merged in `_init_collectors` via `dict.fromkeys`); private/executions/positions stay scoped to `symbols`. Only affects NEW recordings.
    - **#3a integration tests MUST use `wind_down_mode: leave_open`** — `_wind_down` (close_all) does not call `refresh_balances`/`update_equity` and `finalize()` does not rewrite `total_equity`, so read `session.total_equity` after the tick loop, NOT post-`finalize()`. Static-balance limitation: `coin_balances` are frozen at seed; live deposits/withdrawals/spot-trades of the coin during the window are un-modelled (#3a WARN). Empty `collateral_coins` → term is identically zero (USDT-only no-op, acceptance #4).
    - Real-data attribution: `scripts/verify_0065_collateral.py` shows `balance × (mark_end − mark_seed)` against live `totalEquity` drift on a recorder DB. See `docs/features/0065_PLAN.md`.

20. **`claude-code-action` workflow file must match default branch** — The `.github/workflows/claude-code-review.yml` on a PR branch must be identical to the version on `main`. Modify it on `main` first, then all future PRs pick it up. If you change it on a feature branch, the OIDC token validation fails with "Workflow validation failed."
21. **`_margin_ratio` in calculator.py** — Distinguishes `pos is None` (no position, returns 0 silently) from `wallet_balance <= 0` (logs warning then returns 0). This aids debugging when wallet data is missing.
22. **`grid.py __center_grid` rebalancing** — `lowest_buy_price` must be tracked in the loop (not just initialized from `grid[0]`). After `update_grid` changes sides, grid[0] may be WAIT, not BUY. Fixed 2026-04-11.
23. **f-string division in `runner.py _process_fill`** — Decimal division by `session.current_balance` inside f-strings crashes even when debug logging is disabled. Always guard balance divisions with `> 0` check outside the f-string. Fixed 2026-04-11.
24. **`reconciler.py` public trade reconciliation** — Bybit's `/v5/market/recent-trade` only returns the most recent trades; it does NOT support time-range queries. The reconciler logs a warning when fetched data doesn't cover the gap. Execution reconciliation (`get_executions_all`) correctly passes `start_time`/`end_time`. **`reconcile_public_trades` must floor `reconcile_start` at `gap_start`** when `last_persisted_ts >= gap_start` (mirrors execution path) — otherwise post-gap live WS writes advance `get_last_trade_ts` past the outage and the local filter silently skips the entire gap. Path: `apps/event_saver/src/event_saver/reconciler.py`. Fixed 2026-06-30.
25. **`runner.py _execute_intents` stale limits snapshot** — `_execute_intents()` must refresh the `limits` snapshot after each successful `_execute_place_intent()` call. Without this, multiple reduce-only intents in the same batch all check against the same stale snapshot, over-covering the position and causing Bybit 110017 reduce-only rejections. Path: `apps/gridbot/src/gridbot/runner.py`. Fixed 2026-04-11.
26. **Backtest `_should_place_close` must resolve intent qty** — Engine emits `qty=0`; the gate must resolve it via `executor.qty_calculator` before checking `pos_size > (pending + intent_qty)`. Without this, the backtest gate is weaker than live `_is_good_to_place()` and allows over-closing positions. Path: `apps/backtest/src/backtest/runner.py`. Fixed 2026-04-11.
27. **Backtest `_apply_risk_to_qty` must re-round after multiplier** — Base qty is rounded to `qty_step`, but multiplying by the risk multiplier can produce sub-step values (e.g., 0.001 * 0.5 = 0.0005). Must call `instrument_info.round_qty()` after multiplying, matching live `_resolve_qty`. Path: `apps/backtest/src/backtest/runner.py`. Fixed 2026-04-11.

