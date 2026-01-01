# Feature 0001 Code Review (gridcore extraction)

## Summary

`gridcore` is mostly aligned with `docs/features/0001_PLAN.md`: the package exists with the core modules, the strategy logic is exchange-independent, and comparison tests against `bbu_reference/bbu2-master` are present.

However, this review file previously contained a few stale/incorrect findings. This update corrects those items and records the current highest-risk issues found in the implementation.

## Plan Compliance Checklist

### ✅ Matches plan intent

- **Zero exchange dependencies**: No exchange imports under `packages/gridcore/src/gridcore/`.
- **Core extraction completed**: `events.py`, `intents.py`, `grid.py`, `engine.py`, `position.py`, `config.py` exist and follow the intended separation.
- **Comparison tests exist**: `packages/gridcore/tests/test_comparison.py` validates key `Grid` behavior vs `bbu_reference/bbu2-master/greed.py`.

### ✅ Items previously flagged but actually OK

- **`Grid.update_greed()` out-of-bounds flow**: `packages/gridcore/src/gridcore/grid.py` rebuilds and continues side assignment/centering (matches reference behavior).
- **`rebalance_threshold` wiring**: `GridConfig.rebalance_threshold` is passed into `Grid(...)` and used as `REBALANCE_THRESHOLD`.
- **Out-of-bounds comparison coverage**: `packages/gridcore/tests/test_comparison.py` includes an out-of-bounds update test.

### ⚠️ Plan mismatches / missing pieces

- **Events tests**: Plan calls for `packages/gridcore/tests/test_events.py`; currently there is no dedicated events test module.
- **Coverage target**: Plan says `--cov-fail-under=95`, but project docs (`RULES.md`, `packages/gridcore/README.md`) use `80`. If 80 is the project standard now, update the plan (or raise the target).

## Findings (Bugs / Risky Behavior)

### 1) ✅ FIXED: `GridEngine` rebuild path corrupts grid length

**Status: Fixed in grid.py**

In `packages/gridcore/src/gridcore/engine.py`, when there are "too many orders", the engine attempts a rebuild via:

- `self.grid.build_greed(self.last_close)`

~~But `Grid.build_greed()` appends to `self.greed` and does not clear existing entries first.~~

**Fix Applied:** `Grid.build_greed()` now clears `self.greed = []` before building (grid.py:78), preventing grid doubling on rebuilds.

### 2) ✅ FIXED: Duplicate order placement risk

**Status: Fixed in intents.py**

~~`GridEngine` tracks `pending_orders` from `OrderUpdateEvent`, but placement logic does not consult it to suppress re-sending identical placements when an order hasn't yet appeared in `limit_orders`.~~

**Fix Applied:** `PlaceLimitIntent.create()` now generates deterministic `client_order_id` using SHA256 hash of `{symbol}_{side}_{price}_{grid_level}_{direction}` (intents.py:63-65). This allows the execution layer to detect and skip duplicate placement attempts.

## Data Shape / Alignment Risks

- `GridEngine` assumes Bybit-style limit order dictionaries with camelCase keys like `orderId`, `price`, and `side`. If adapters provide snake_case or nested payloads (e.g. `{data: {...}}`), intent generation/cancellation will break.
- `limit_prices.get(greed['price'])` relies on exact float equality. This matches the reference code, but remains sensitive to string→float parsing and rounding differences across adapters.

Recommendation:
- Document the required input schema for `limit_orders` clearly (or introduce a small normalization layer/type).

## Position Module Fidelity Notes

`PositionRiskManager` is presented as extracted from `bbu2-master/position.py`, but there are important behavioral differences:

- In the reference, the “moderate liquidation risk” branches adjust multipliers on the *opposite* position (via `self.__opposite.set_amount_multiplier(...)`). In `gridcore`, those branches adjust the current manager’s `amount_multiplier` instead.
- `wallet_balance` is accepted by `calculate_amount_multiplier(...)` but not used; in the reference, margin is derived from `positionValue / wallet_balance`.

If exact behavioral parity is a goal for `position.py`, this needs reconciliation and stronger parity tests.

## Unit Test Review

Strengths:
- `packages/gridcore/tests/test_comparison.py` is the right approach for parity and already covers out-of-bounds grid updates.
- Engine tests cover key behaviors (placement eligibility, side mismatch cancellation, outside-grid cancellation, rebuild trigger).

Gaps / improvements:
- Add `test_events.py` to lock `EventType` enforcement in event models.
- Add tests that cover idempotency/de-duping behavior for `GridEngine` (or document that the execution layer must dedupe).
- If position logic is relied on, add parity tests against reference or explicitly mark the module as “approximate / refactored semantics”.

## Style / Maintainability Notes (Minor)

- The phrase “pure function” is misleading for `GridEngine.on_event()` because it mutates internal state (even though it has no external side effects). Consider wording like “side-effect free (no I/O)”.
- `PositionRiskManager._apply_*` signatures include unused parameters (e.g. `opposite_margin`). Either use them or remove them for clarity.

