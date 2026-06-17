# Order Identity Design

## Overview

Orders in the grid trading system are identified by a deterministic `client_order_id` generated from a subset of order parameters. This document explains the design rationale and implementation details.

## Current Design (Updated 2026-02-06)

### Identity Parameters

Orders are uniquely identified by:
```python
_IDENTITY_PARAMS = ['symbol', 'side', 'price', 'direction']
```

**NOT included in identity:**
- `qty` - Determined by execution layer based on wallet balance
- `reduce_only` - Order flag, not part of order identity
- `grid_level` - **Tracking metadata only (changed 2026-02-06)**

### Client Order ID Generation

```python
def create_client_order_id(symbol, side, price, direction):
    id_string = f"{symbol}_{side}_{price}_{direction}"
    return hashlib.sha256(id_string.encode()).hexdigest()[:16]
```

**Result:** Same `(symbol, side, price, direction)` → Same `client_order_id`

### Strategy Namespacing (Feature 0080, issue #183, 2026-06-17)

`PlaceLimitIntent.create` accepts an OPTIONAL `strat_id` that namespaces the
hash so two strategies on the same `(account, symbol)` no longer collide on the
deterministic prefix:

```python
# strat_id is a SALT, not an _IDENTITY_PARAMS entry:
if strat_id is not None:
    id_string = f"{strat_id}_{symbol}_{side}_{price}_{direction}"
else:
    id_string = f"{symbol}_{side}_{price}_{direction}"   # pre-0080, byte-for-byte
client_order_id = sha256(id_string.encode()).hexdigest()[:16]
```

- **`strat_id` is a salt, NOT in `_IDENTITY_PARAMS`.** Adding it to the param set
  would also feed dedup/equality; instead it is applied only inside `create`, so
  the survive-rebalance property and intra-engine dedup are unchanged.
- **`None` default = old hash byte-for-byte.** Existing callers and historical
  recorded rows keep the same id; only the 3 production call sites
  (`engine.py`, `runner.py` ×2) thread `strat_id`.
- **Wire form unchanged:** `{hash16}-{millis}` = 30 chars. Bybit caps
  `orderLinkId` at **36 chars** (`gridbot.order_link_id._BYBIT_ORDER_LINK_ID_MAX`);
  `make_order_link_id` raises if exceeded. `extract_client_order_prefix`
  (partition-on-first-`-`) is unchanged.

#### Collision rules

| symbol | side | price | direction | strat_id | → client_order_id |
|--------|------|-------|-----------|----------|-------------------|
| = | = | = | = | = | SAME (deterministic survival) |
| = | = | = | = | ≠ | DIFFERENT (the #183 fix) |
| any one differs | | | | any | DIFFERENT |

#### Replay / comparator boundary rule

The comparator joins live vs backtest trades on `client_order_id`. Recorded LIVE
orders were salted with the live `strat_id`, so **replay MUST salt with the same
live id**. The recording's `strat_id` is NOT stored on any `Order` /
`PrivateExecution` / `Strategy` DB row, so it is supplied via config:
`apps/replay/src/replay/engine.py` resolves the engine `strat_id` with precedence
`ReplayStrategyConfig.strat_id` → `seed.strat_id` (when seeding) → synthetic
`replay_{symbol}` (no recorded orders to match). When comparing a **blank-start**
replay against recorded executions, set `strategy.strat_id` to the recording's
live id — otherwise the synthetic fallback diverges and the match rate collapses.
A replay over a PRE-0080 window uses `strat_id=None`, reproducing the old hash.

## Key Design Decision: grid_level Excluded from Hash

### Rationale

**Problem Solved:**
When the grid rebalances via `center_grid()`, the grid array shifts:
- Grid levels change (e.g., level 10 becomes level 9)
- But prices may stay the same
- Old design: Different `grid_level` → Different `client_order_id` → Order canceled and replaced
- New design: Same price → Same `client_order_id` → Order survives

**Example:**
```python
# Before rebalancing:
grid.grid[10] = {'price': 99000, 'side': 'Buy'}
intent1 = PlaceLimitIntent.create(..., price=99000, grid_level=10)
# client_order_id = hash("BTCUSDT_Buy_99000_long")

# After center_grid() shifts array:
grid.grid[9] = {'price': 99000, 'side': 'Buy'}
intent2 = PlaceLimitIntent.create(..., price=99000, grid_level=9)
# client_order_id = hash("BTCUSDT_Buy_99000_long")  # SAME!

# Order survives rebalancing - no cancellation needed
```

### Benefits

1. **Fewer Order Replacements** - Orders survive `center_grid()` rebalancing
2. **Lower Exchange Load** - Fewer cancel/place API calls
3. **Better Fill Rates** - Orders stay in exchange queue instead of losing position
4. **Tracking Preserved** - `grid_level` field still exists for analytics

### Trade-offs

**Advantages:**
- ✅ Orders survive grid rebalancing
- ✅ More efficient order management
- ✅ Still have full tracking capability

**Considerations:**
- ⚠️ **Price uniqueness required** - Duplicate prices in grid would cause collisions
- ⚠️ **Safety check added** - `build_grid()` validates no duplicate prices

## Safety Mechanisms

### 1. Duplicate Price Detection

```python
# In grid.py build_grid():
prices = [g['price'] for g in self.grid]
if len(prices) != len(set(prices)):
    raise ValueError(f"Grid contains duplicate prices: {duplicates}")
```

**When would duplicates occur?**
- Extreme edge case: Very small prices (0.0001) with coarse tick_size (0.00001)
- Grid step too small relative to tick_size rounding
- Example: 0.0001 * 0.998 rounds to 0.0001, same as 0.0001 * 1.002

**Mitigation:**
- Real-world scenarios use appropriate tick_size relative to price
- BTC ($100,000) with tick_size 0.1 → No issues
- Safety check catches configuration errors early

### 2. Outside-Grid Cancellation

```python
# In engine.py:
grid_price_set = {g['price'] for g in self.grid.grid}
outside_limits = [
    limit for limit in current_orders
    if limit['price'] not in grid_price_set
]
intents.extend(self._cancel_all_limits(outside_limits, 'outside_grid'))
```

Orders outside the grid range are canceled regardless of `client_order_id` matching. This handles grid rebuilds with different anchor prices.

## Tracking and Analytics

### grid_level Field Still Available

```python
@dataclass(frozen=True)
class PlaceLimitIntent:
    ...
    client_order_id: str  # Based on (symbol, side, price, direction)
    grid_level: int       # Preserved for tracking, NOT in hash
```

**Use Cases:**
- **Backtesting reports:** "Grid level 25 (center) filled 50 times"
- **Analytics:** "Orders at grid edges rarely fill"
- **Comparison:** Match bbu2 vs gridcore by grid position
- **Debugging:** "Order at grid_level=10 never filled"

### Example Analysis

```python
# Group fills by grid_level:
fills_by_level = defaultdict(int)
for order in filled_orders:
    fills_by_level[order.grid_level] += 1

# Output:
# grid_level=0:  5 fills (bottom edge)
# grid_level=25: 50 fills (center - high activity)
# grid_level=49: 3 fills (top edge)
```

## Implementation History

### Original Design (Before 2026-02-06)

```python
_IDENTITY_PARAMS = ['symbol', 'side', 'price', 'grid_level', 'direction']
```

- Orders tied to specific grid level
- Grid rebalancing forced order replacement
- More churn, but guaranteed grid structure consistency

### Current Design (2026-02-06)

```python
_IDENTITY_PARAMS = ['symbol', 'side', 'price', 'direction']
```

- Orders survive grid rebalancing
- Safety checks prevent price collisions
- Full tracking capability retained

### Strategy Namespacing (Feature 0080, 2026-06-17)

```python
# strat_id salted into the hash INPUT (not _IDENTITY_PARAMS); None = pre-0080 hash
id_string = f"{strat_id}_{symbol}_{side}_{price}_{direction}"  # when strat_id set
```

- Two strats on the same `(account, symbol)` get DISTINCT prefixes (issue #183).
- Resolves the orderLinkId-prefix reason in `validate_no_shared_symbol`; that
  guard STAYS (positionIdx / cancel-on-mismatch sharing remains a blocker).
- Wire form + `extract_client_order_prefix` unchanged; 36-char Bybit limit guarded.

## Testing

### Tests Added

1. **`test_grid_level_does_not_affect_id()`**
   - Validates same price → same ID regardless of grid_level
   - Confirms grid_level field still preserved

2. **`test_build_grid_no_duplicate_prices()`**
   - Validates no price collisions after grid build
   - Ensures safety mechanism works

3. **Updated comparison tests**
   - `test_deterministic_client_order_id()` - Changed expectation
   - `test_extreme_prices_match_original()` - Handles edge case

### Test Coverage

- All 219 gridcore tests pass
- Safety check catches duplicate price scenarios
- Extreme edge case (0.0001 price) properly handled

## Migration Notes

### For Existing Systems

**No migration needed** - This is a pure implementation improvement:
- Order behavior changes (orders survive rebalancing)
- But external interface unchanged
- `client_order_id` still deterministic
- `grid_level` still available for tracking

### For New Features

When adding new parameters to `PlaceLimitIntent`:
1. Decide: Does this parameter affect order identity?
2. **If YES:** Add to `_IDENTITY_PARAMS`
3. **If NO:** Keep as field but don't add to params

**Examples:**
- `time_in_force` → Would affect identity (different order type)
- `trigger_price` → Would affect identity (different trigger)
- `notes` → Would NOT affect identity (metadata)

## References

- Implementation: `packages/gridcore/src/gridcore/intents.py`
- Safety check: `packages/gridcore/src/gridcore/grid.py`
- Tests: `packages/gridcore/tests/test_engine.py`
- RULES.md: Common Pitfalls #10
