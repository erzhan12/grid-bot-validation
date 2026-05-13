# Feature 0040 — Code Review (Round 2)

Branch: `feature/0040-low-margin-equal-boost-config`
Plan: `docs/features/0040_PLAN.md`
Scope: wire `increase_same_position_on_low_margin` from YAML → `StrategyConfig` → `RiskConfig`. Mechanical pass-through only; no `gridcore` semantics changed.

## What changed since Round 1

- ✅ **End-to-end YAML coverage added** — `apps/gridbot/tests/test_config.py:298,314` now sets `increase_same_position_on_low_margin: True` in `config_data` and asserts on the loaded strategy.
- ✅ **ASCII/Unicode reconciled** — `apps/gridbot/src/gridbot/config.py:59` description now uses ASCII `x2` / `x0.5`, matching `apps/gridbot/conf/gridbot.yaml.example:23`.
- ✅ **Operator comment added** — `conf/gridbot_test.yaml:16` now carries `# Continuous own-side x2 boost while equal positions AND total_margin < 3.` above the flag.
- ✅ **RULES.md entry added** — bullet appended to `### Position Risk Module (`position.py`)` documenting the gridbot-only wire-through and the deliberate live↔backtest divergence.

## Verification

- `uv run pytest apps/gridbot/tests/test_config.py apps/gridbot/tests/test_runner.py -q` — **176 passed in 0.16s**.
- `git diff --stat` — 7 files, +83/-1 lines. Matches plan scope.

## Summary

No **Critical**, no **Warning**, no remaining **Suggestion**. Implementation complete and reviewed.

### Operator-risk note (not a vulnerability, unchanged from Round 1)

`conf/gridbot_test.yaml:16-18` flips the **live mainnet** LTCUSDT bot into the 2x-on-low-margin boost regime. With `min_total_margin: 3` and current `total_margin ≈ 2.55`, the condition is continuously true; every time `position_ratio` lands in the open interval `(0.94, 1.05)` one side's `amount_multiplier` flips to `2.0`. The new inline comment now makes this visible to operators reading the file. The plan's "Behavioural caveat" documents intent; `max_margin: 5.0` is the only remaining brake on continuous boosting.

## Per-category findings

### 1. Code Quality — clean

- `apps/gridbot/src/gridbot/config.py:55-61` — Pydantic Field placed correctly after `min_total_margin`, follows the `Field(default=..., description=...)` pattern of adjacent fields.
- `apps/gridbot/src/gridbot/runner.py:201-203` — kwarg added in same order as the `RiskConfig` constructor's field order; multi-line wrap style matches surrounding code.
- `apps/gridbot/tests/test_notifier.py:5` — unused `Mock` import removed; only `MagicMock` is referenced. Acceptable one-line lint cleanup in a touched test suite.

### 2. Security — clean

- No shell/SQL/HTML user-input sinks introduced. No new endpoints/handlers.
- No hardcoded secrets. `${DATABASE_URL}` interpolation unchanged.
- Pydantic strictly validates `bool`; `test_config.py:86-94` covers rejection of non-coercible types.
- Trust boundary unchanged (YAML loaded by operator, not network input).

### 3. Performance — clean

- One-time Pydantic field construction and one-time kwarg pass-through at runner init.
- Hot-path impact in `packages/gridcore/src/gridcore/position.py:400`: a single attribute read on `self.risk_config` selecting between two pre-existing `set_amount_multiplier(...)` branches. No new allocations, queries, async calls, or I/O.

### 4. Testing — strong

- `apps/gridbot/tests/test_config.py:64-94` — three direct-construction tests: default off, explicit true, invalid type (`[]`, Pydantic v2 rejects).
- `apps/gridbot/tests/test_config.py:298,314` — YAML round-trip now covered via `load_config()` path.
- `apps/gridbot/tests/test_runner.py:169-200` — both `_long_position` and `_short_position` asserted in both True and default-False states. Uses `model_copy(update=...)` (correct for Pydantic v2 convention).
- All three layers of the wire-through (YAML key → StrategyConfig field → runner kwarg → RiskConfig attr) are now tested.

### 5. Documentation — consistent

- `config.py:59` description and `gridbot.yaml.example:23` comment both ASCII now — consistent.
- `conf/gridbot_test.yaml:16` carries an operator-facing comment explaining the continuous-boost regime.
- `default=False` preserves prior behaviour; existing YAMLs without the field remain green. No migration note required.
- RULES.md updated with a bullet under `### Position Risk Module (`position.py`)`.

## Out-of-scope follow-ups (documented in plan, deliberately deferred)

1. `apps/backtest/src/backtest/config.py:50` + `apps/backtest/src/backtest/runner.py:151-155` — same 4-arg `RiskConfig` pattern; live↔backtest semantic divergence until wired.
2. `apps/pnl_checker/src/pnl_checker/config.py:83` + `apps/pnl_checker/src/pnl_checker/main.py:82-86` — same pattern; read-only analysis tool.

Both should be tracked as separate tickets immediately after 0040 merges.
