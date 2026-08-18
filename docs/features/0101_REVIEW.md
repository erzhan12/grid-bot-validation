# Feature 0101 — External Code Review Trail

Feature: subtract seed-time unrealized PnL (U0) from seeded replay balance/equity baselines (issue #244).
Branch: `feature/0101-u0-seed-double-count`. Reviewed diff = `git diff HEAD` (9 files: 3 src, 3 test, 3 docs).

## Round 1 — codex (gpt-5.6-sol) + cursor (agent), read-only

Both engines returned **NO P1/P2 FINDINGS**. Cursor produced a full verification log (all ~20 claims PASS: nullable fields + defaults, loader pass-through with no new query/at_ts bind, U0 hierarchy + fail-loud, mark-formula sign vs `gridcore/pnl.py`, Decimal-only, single-engine both-baselines-before-session, multi asymmetry TAB−u0 / coin_balance unchanged, defaulted `u0=0` keeps 0095 tests, Cycles 1–6 drive production `run()` with hand literals).

### P3 findings (non-blocking) — triage

| # | Engine | Finding | Verdict | Action |
|---|--------|---------|---------|--------|
| 1 | cursor | Cycle-3 equity-literal tests still seed the fixture's working Buy@99000 (plan: seed no working orders); safe today but latent contamination risk | ACCEPT | Delete all `seed-run` orders in `_stamp_long_upl` (evolution tests) and in the full-close test before adding the reduce-only close. Tests still pass with identical literals → flatness was genuine. |
| 2 | cursor | `test_seed_upl_overstates_equity_pre_fix` is a post-fix assertion; name reads as pre-fix reproduction | ACCEPT | Renamed → `test_seed_upl_corrects_both_single_engine_baselines`. |
| 3 | cursor | `compute_seed_upl` docstring says "both baselines overstated" — true single-engine only (multi overstates TAB only) | ACCEPT | Tightened docstring: helper only sums; caller decides where to subtract (single=both, multi=TAB only). |
| 4 | codex | Module docstring (`test_engine_seed.py:3`) claims tests "stop short of running ticks" — contradicted by new `run()` tests | ACCEPT | Clarified: original Phase-3 classes don't run ticks; the 0101 `TestSeedUplCorrection` drives production `run()`. |
| 5 | codex | Wallet-seed-None fallback tested via hand-built session; production `run()` `wallet_seed is not None` guard lacks its own regression | REJECT (accepted gap) | Non-blocking P3. The None path simply skips `compute_seed_upl` (no correction); the flat-seed no-op test already drives `run()` with a present wallet seed + `U0=0`. Low risk; not worth a bespoke fixture. |

No P1/P2 raised → no code-behavior fixes needed; the five P3s were doc/name/test-hygiene only.

## Verification after fixes
- `uv run pytest apps/replay/tests/{test_engine_seed,test_multi_engine,test_snapshot_loader}.py` → **102 passed**.
- Full `make test` (pre-review) → EXIT 0, coverage 91% (gates ≥88 total / ≥80 gridcore).
- `make lint` → clean.
- Empirical `make live-check` (read-only): **solusdt_test PASS** (U0 fired live, trade parity to-the-cent); ltcusdt_test SKIP (no execs in window).

## Result
SUCCESS — zero valid P1/P2, tests + lint green. Four P3s fixed (hygiene), one rejected as accepted gap.
