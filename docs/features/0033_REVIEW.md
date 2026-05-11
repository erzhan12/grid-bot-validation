# Feature 0033 — Code review (Round 4, post-fix verification)

## Summary

Round 3 raised five P3 polish findings (N1–N5) and one RULES.md
documentation gap. **All six are addressed in Round 4.** A fourth-pass
re-read of the codebase did not surface any new P0/P1/P2 issues. The
single net new observation is that the previously-flagged latent
`last_price = Decimal('0')` phantom-fill (N2 in R3) is **now actively
defended in code AND covered by a regression test**, going further than
R3 even suggested.

**Verdict:** Code is ready to ship pending the two manual parity
smokes from the plan's verification block.

**Test status:** 194 targeted tests pass (was 152 in R3; +42 from
recent additions including the matrix expansion and defensive
guards). Full suite: `3 failed, 574 passed` — the 3 failures are in
`test_risk_limit_info.py` and pre-exist on `main` (verified). No new
ruff debt (7 errors on `main`, 7 here, all pre-existing).

---

## Round-3 findings — all closed

### N1 — Class docstring describes only strict_cross — ✅ **FIXED**

**File:** `apps/backtest/src/backtest/fill_simulator.py:42-56`

```python
class TradeThroughFillSimulator:
    """Configurable fill model for simulated limit orders.

    Supported modes:
    - strict_cross: conservative default; BUY below limit, SELL above limit.
      Exact limit touches do not fill because queue position and volume are
      unknown.
    - trade_through_at_limit: last-price model that includes exact limit
      touches.
    - book_touch: L1-aware parity mode; BUY when ask touches/crosses limit,
      SELL when bid touches/crosses limit, with last-price-at-limit fallback
      only when L1 data is unavailable.

    Fill price is always the limit price.
    """
```

All three modes are now described accurately. The misleading
"At limit price, order does NOT fill" universal claim is replaced with
mode-specific bullets. Clean.

### N2 — Latent `last_price = 0` phantom-fill — ✅ **FIXED (with regression test)**

**Fix locations:**
- `fill_simulator.py:145-146` (strict_cross guard)
- `fill_simulator.py:161-162` (trade_through_at_limit guard)

```python
def _should_fill_strict_cross(self, side, limit_price, current_price):
    if current_price <= 0:
        return False
    ...

def _should_fill_at_limit(self, side, limit_price, current_price):
    if current_price <= 0:
        return False
    ...
```

The book_touch fallback path goes through `_should_fill_at_limit`, so
it inherits the guard transitively. All three modes now refuse to fill
when `last_price <= 0`.

**Regression test added:**
`test_non_positive_last_price_never_fills`
(`apps/backtest/tests/test_fill_simulator.py`), parameterized across
all three modes:

```python
ticker = TickerEvent(
    event_type=EventType.TICKER,
    symbol="LTCUSDT",
    exchange_ts=ts,
    local_ts=ts,
    # last_price OMITTED — defaults to Decimal('0')
)
buy_result = simulator.check_fill(_order("Buy"), ticker)
sell_result = simulator.check_fill(_order("Sell"), ticker)
assert buy_result.should_fill is False
assert sell_result.should_fill is False
```

Pre-fix this would have flipped BUY to True for strict_cross /
trade_through_at_limit (and book_touch via fallback). Symmetric to the
P2 fix for bid/ask in Round 2. **0033 leaves the codebase strictly
more correct than it found it on the strict_cross path** — a
pre-existing latent bug is now closed as a side benefit.

### N3 — Implicit fall-through in `_should_fill` mode dispatch — ✅ **FIXED**

**File:** `apps/backtest/src/backtest/fill_simulator.py:124-136`

```python
match self._mode:
    case FillMode.STRICT_CROSS:
        return self._should_fill_strict_cross(
            side, limit_price, snapshot.last_price,
        )
    case FillMode.TRADE_THROUGH_AT_LIMIT:
        return self._should_fill_at_limit(
            side, limit_price, snapshot.last_price,
        )
    case FillMode.BOOK_TOUCH:
        return self._should_fill_book_touch(side, limit_price, snapshot)

raise ValueError(f"Unsupported fill mode: {self._mode}")
```

Exact recommendation from R3 applied: `match` / `case` with all known
modes enumerated, plus an explicit `raise ValueError` for any future
mode that lands without dispatch coverage. Future contributors cannot
silently fall through to `book_touch` semantics anymore. Clean.

### N4 — Honest type signature for `check_fills` — ✅ **FIXED**

**File:** `apps/backtest/src/backtest/order_manager.py:218-249`

Three `@overload` decorators now document the legitimate call shapes
for static analysis:

```python
@overload
def check_fills(
    self,
    market: TickerEvent,
    timestamp: Optional[datetime] = None,
    symbol: Optional[str] = None,
) -> list[ExecutionEvent]: ...

@overload
def check_fills(
    self,
    market: Decimal,
    timestamp: datetime,                   # REQUIRED in this overload
    symbol: Optional[str] = None,
) -> list[ExecutionEvent]: ...

@overload
def check_fills(
    self,
    *,
    current_price: Decimal,
    timestamp: datetime,                   # REQUIRED in this overload
    symbol: Optional[str] = None,
) -> list[ExecutionEvent]: ...

def check_fills(...):                       # runtime impl with Union+None
    ...
```

Static type checkers now see three precise signatures: `TickerEvent`
input (timestamp defaults to `market.exchange_ts`), `Decimal` input
(timestamp required positionally), or legacy keyword `current_price`
(timestamp required). The runtime impl is dispatch glue. This matches
the standard Python pattern for backward-compatible signature
evolution.

### N5 — Test inconsistency in book_touch fallback test — ✅ **FIXED (different shape)**

**File:** `apps/backtest/tests/test_order_manager.py:355-377`

```python
def test_book_touch_falls_back_through_order_manager_bare_decimal(
    self,
    order_manager,                          # uses shared fixture
    sample_timestamp,
):
    """BOOK_TOUCH degrades to at-limit semantics for bare Decimal input."""
    order_manager.fill_simulator = TradeThroughFillSimulator(mode=FillMode.BOOK_TOUCH)
    ...
```

Resolution differs from R3's recommendation (no new `book_touch_order_manager`
fixture introduced) but achieves the goal: the test now reuses the
shared `order_manager` fixture and just swaps in the BOOK_TOUCH
simulator inline. Less fixture proliferation, same test isolation.
Reasonable trade-off — accept.

### RULES.md gap — ✅ **FIXED**

**File:** `RULES.md:1649`

Added bullet under section 3:

> `BacktestOrderManager.check_fills(TickerEvent(...))` is always scoped
> to the ticker's own symbol; the legacy bare-Decimal path preserves
> all-symbol scanning when no `symbol` filter is supplied.

The asymmetric symbol-default contract from Step 2 of the plan is now
documented in RULES.md as recommended in R3.

---

## Round-1 + Round-2 findings — confirmed still closed

| Finding | Origin | Status |
|---|---|---|
| P2 phantom-fill on `TickerEvent(bid1=0, ask1=0)` | R1 | ✅ Fixed via `_normalize_l1_price`; regression test passes |
| P3 `current_price` kwarg undocumented | R1 | ✅ Docstring explicitly marks as deprecated alias |
| P3 redundant `mkdir` in `main.py` | R1 | ✅ Moved before `export_all` |
| P3 misleading `fills` var name in test | R1 | ✅ Renamed to `post_fill_intents` |

---

## Round-4 deep re-pass — observations

A fourth-pass re-read found **no new defects**. Notable observations:

1. **`@overload` semantics consistency check.** The runtime impl accepts
   `market: TickerEvent | Decimal | None = None` with a runtime `raise
   ValueError` if both `market` and `current_price` are `None`. The
   overloads don't expose this `None` shape — good, because it's an
   error case, not a valid call form. Static type checkers will reject
   `check_fills()` (no args) as ambiguous, which matches runtime
   behavior.

2. **Defense-in-depth on guard placement.** The `current_price <= 0`
   guard is duplicated in `_should_fill_strict_cross` AND
   `_should_fill_at_limit`. Could be lifted to the `_should_fill`
   dispatcher for DRY, but the current placement is **safer**: it
   protects each private helper against direct callers (current or
   future). Acceptable redundancy.

3. **`match` statement enforces exhaustiveness only at runtime.** Python
   `match`/`case` doesn't have compile-time exhaustiveness checking like
   Rust. The `raise ValueError` after the match is the correct
   compensating pattern. No further hardening needed.

4. **The book_touch fallback path has no explicit "L1 missing"
   telemetry.** When `bid1`/`ask1` is normalized to `None` and the
   simulator silently degrades to `trade_through_at_limit`, there is no
   log line or counter. For replay parity smoke this is fine (callers
   know they configured `book_touch`), but for production-style
   diagnostic visibility it might be worth a `logger.debug(...)` in
   `_should_fill_book_touch`. Not blocking; flag for follow-up if
   parity-smoke results ever look suspicious for L1-missing reasons.

5. **`_resolve_run` always returns concrete values.** `ReplayResult`
   now requires non-Optional `run_id`, `start_ts`, `end_ts`. Verified:
   `_resolve_run` raises if it can't resolve (e.g., no recording run
   found, no time range available), so the contract is honored.

6. **`fill_mode` serialization.** `summary.json` writes
   `result.fill_mode.value` (the string), not the enum object. Correct —
   JSON can't natively serialize enums.

7. **Comparator metadata is read-once.** `ComparatorReporter.__init__`
   stashes `metadata or {}`. If the caller mutates the dict after
   construction, it'll be reflected in the export (defensive copy not
   made). Not a concern in current callers — replay's `main.py` builds
   the dict inline and never mutates — but worth a note if metadata
   ever gets passed from a longer-lived owner.

---

## Test suite status

```
uv run pytest apps/backtest/tests/test_fill_simulator.py
              apps/backtest/tests/test_order_manager.py
              apps/backtest/tests/test_runner.py
              apps/replay/tests/
              apps/comparator/tests/test_reporter.py -q
→ 194 passed in 0.30s    (was 152 in R3 → +42 from added matrix cases
                          and the parameterized last_price=0 regression)
```

Full suite:
```
uv run pytest apps/backtest/ apps/replay/ apps/comparator/ -q
→ 3 failed, 574 passed in 1.43s
```

The 3 failures in `test_risk_limit_info.py` pre-exist on `main`
(confirmed in R2 via `git stash`).

**Test growth on this branch:**

| File | Tests added |
|---|---|
| `test_fill_simulator.py` | Matrix expansion (3 modes × 6 cases × 2 sides = 36), 2 bare-Decimal fallback, 1 zero-bid/ask regression, 1 zero-last-price regression × 3 modes |
| `test_order_manager.py` | symbol-scope, symbol-kwarg-ignored, decimal-timestamp-required, book_touch via order_manager |
| `test_runner.py` | book_touch end-to-end integration |
| `test_config.py` | 4 round-trip cases for `fill_simulator` block |
| `test_engine.py` | ReplayResult metadata assertions |
| `test_reporter.py` | metadata-empty + metadata-with-fill_mode |

Total new tests roughly: ~50, mostly parameterized.

---

## Lint status

```
uv run ruff check apps/backtest/src apps/replay/src apps/comparator/src
→ Found 7 errors.   (all F401 unused imports in pre-existing files;
                     verified zero new errors introduced by 0033)
```

---

## Plan compliance — all 27 plan items verified

(All items already enumerated and verified in R3 review; status
unchanged in R4 — every plan-mandated change is present in code.)

---

## Pre-merge work remaining

| Item | Status |
|---|---|
| P0/P1/P2 findings | ✅ 0 open |
| R1 polish (P3 × 3) | ✅ all closed (R2) |
| R3 polish (P3 × 5) | ✅ all closed (R4) |
| R3 RULES.md gap | ✅ closed (R4) |
| R4 fresh-pass review | ✅ no new defects |
| `uv run pytest apps/backtest/ apps/replay/ apps/comparator/ -q` | ✅ 574 pass + 3 pre-existing failures |
| `uv run ruff check apps/backtest/src apps/replay/src apps/comparator/src` | ✅ 7 pre-existing errors, 0 new |
| `git diff --exit-code -- bbu_reference` | ✅ clean |
| **Manual parity smoke: `mode: book_touch`** — target `match_rate ≥ 0.95`, `pnl_correlation ≥ 0.99`, `live_only ≤ 4`, `backtest_only / matched ≤ 1-2%` | ⏳ pending |
| **Manual backward-compat parity smoke (default config)** — must reproduce Run 2 baseline (`match_rate = 0.913`, `live_only = 12`, `backtest_only = 0`) | ⏳ pending |

---

## TL;DR — findings across all four rounds

| Severity | R1 | R2 | R3 | R4 |
|---|---|---|---|---|
| P0 | 0 | 0 | 0 | 0 |
| P1 | 0 | 0 | 0 | 0 |
| P2 | 1 (phantom-fill on default zero L1) | 0 (fixed) | 0 | 0 |
| P3 | 3 | 0 (all closed) | 5 (docs, last_price=0, dispatch fall-through, type hint, fixture) | 0 (all closed) |
| Docs | 0 | 0 | 1 (RULES.md asymmetric symbol) | 0 (closed) |

**Cumulative cleanup:** 1 P2 + 8 P3 + 1 docs = 10 findings; all 10
closed across R2 + R4. No findings remain open.

**Implementation is functionally and stylistically ship-ready.** The
codebase is in better shape than before this feature — the side-effect
last_price=0 hardening (N2) closes a pre-existing latent bug that was
unrelated to 0033's primary scope but uncovered during review.

The only remaining work is the two manual parity smokes from the
plan's verification block. Recommended execution:

```bash
# 1) book_touch parity (replay_ltcusdt_phase4.yaml already has mode: "book_touch")
uv run python -m replay.main --config apps/replay/conf/replay_ltcusdt_phase4.yaml
cat results/replay_ltcusdt_phase4/validation_metrics.csv

# 2) backward-compat (comment out the fill_simulator block in the yaml, re-run)
# Expected: match_rate = 0.913, live_only = 12 — same as Run 2 baseline.
```

Document both runs' metrics inline at the bottom of this file (or in
`0033_RESULTS.md`) and the PR is ready.
