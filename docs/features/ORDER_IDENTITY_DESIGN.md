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
