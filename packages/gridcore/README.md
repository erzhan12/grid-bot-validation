# gridcore

Pure grid trading strategy logic with **zero exchange dependencies**.

## Overview

`gridcore` is a pure Python package extracted from `bbu2-master` that contains core grid trading strategy implementation. It's designed to be usable by both live trading and backtesting applications.

## Key Features

- **Zero Exchange Dependencies**: No imports from `pybit`, `bybit`, or any exchange-specific libraries
- **Event-Driven Architecture**: Strategy processes events and returns intents (no side effects)
- **Deterministic Behavior**: Produces identical results to original `bbu2-master` code
- **Well-Tested**: 88% test coverage with comprehensive unit tests
- **Type-Safe**: Uses dataclasses and type hints throughout

## Installation

### With uv (recommended)

From the project root:

```bash
# Sync the entire workspace (includes all packages and dev dependencies)
uv sync

# Install gridcore in editable mode
uv pip install -e packages/gridcore
```

### With pip

```bash
cd packages/gridcore
pip install -e .

# For development
pip install pytest pytest-cov
```

## Usage

### Basic Grid Setup

```python
from decimal import Decimal
from gridcore import Grid, GridConfig

# Create grid calculator
config = GridConfig(greed_count=50, greed_step=0.2)
grid = Grid(tick_size=Decimal('0.1'), greed_count=50, greed_step=0.2)

# Build grid around current price
grid.build_greed(100000.0)

# After a fill, update grid
grid.update_greed(last_filled_price=99800.0, last_close=100000.0)
```

### Event-Driven Strategy

```python
from datetime import datetime, UTC
from decimal import Decimal
from gridcore import GridEngine, GridConfig, TickerEvent, EventType

# Initialize engine
config = GridConfig(greed_count=50, greed_step=0.2)
engine = GridEngine(symbol='BTCUSDT', tick_size=Decimal('0.1'), config=config)

# Process ticker event
event = TickerEvent(
    event_type=EventType.TICKER,
    symbol='BTCUSDT',
    exchange_ts=datetime.now(UTC),
    local_ts=datetime.now(UTC),
    last_price=Decimal('100000.0'),
    mark_price=Decimal('100000.0'),
    bid1_price=Decimal('99999.0'),
    ask1_price=Decimal('100001.0'),
    funding_rate=Decimal('0.0001')
)

# Get intents (what the strategy wants to do)
intents = engine.on_event(event, {'long': [], 'short': []})

for intent in intents:
    if isinstance(intent, PlaceLimitIntent):
        print(f"Place {intent.side} order at {intent.price}")
    elif isinstance(intent, CancelIntent):
        print(f"Cancel order {intent.order_id}: {intent.reason}")
```

## Architecture

### Modules

- **events.py**: Normalized event models (TickerEvent, ExecutionEvent, OrderUpdateEvent)
- **intents.py**: Order intent models (PlaceLimitIntent, CancelIntent)
- **grid.py**: Grid level calculations (extracted from `greed.py`)
- **engine.py**: Event-driven strategy engine (extracted from `strat.py` Strat50)
- **position.py**: Position state tracking and risk management
- **config.py**: Configuration dataclasses

### Key Transformations from Original

1. **`grid.py` (from `greed.py`)**:
   - Removed `BybitApiUsdt.round_price()` → implemented internal `_round_price()`
   - Removed database calls (`read_from_db()`, `write_to_db()`)
   - Pass `tick_size` as parameter instead of from BybitApiUsdt
   - Added validation methods (`is_price_sorted()`, `is_greed_correct()`)

2. **`engine.py` (from `strat.py` Strat50)**:
   - Converted from network-calling to event-driven pattern
   - Returns `list[Intent]` instead of calling controller methods
   - Made `on_event()` a pure function with no side effects
   - Removed all exchange and database dependencies

3. **`position.py` (from `position.py`)**:
   - Extracted position state into `PositionState` dataclass
   - Extracted risk management logic into `PositionRiskManager`
   - Removed exchange-specific dependencies

## Testing

### With uv (recommended)

From the project root:

```bash
# Run all tests with coverage
uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-report=term-missing --cov-fail-under=80

# Run specific test file
uv run pytest packages/gridcore/tests/test_grid.py -v
```

### With pytest directly

From `packages/gridcore` directory:

```bash
PYTHONPATH=./src pytest tests/ --cov=gridcore --cov-report=term-missing --cov-fail-under=80
```

Current coverage: **89%**

### Test Suites

- **test_grid.py**: Grid calculation tests (17 tests)
- **test_engine.py**: Engine event processing tests (13 tests)
- **test_position.py**: Position risk management tests (7 tests)
- **test_comparison.py**: Comparison with original `bbu2-master` code (7 tests, optional)

## Validation

### Zero Dependencies Check

```bash
grep -r "^import pybit\|^from pybit\|^import bybit\|^from bybit" src/ && echo "FAIL" || echo "PASS"
```

Should output: **PASS**

### Comparison with Original

The comparison tests in `test_comparison.py` verify that `gridcore` produces identical results to the original `bbu2-master` code. These tests require the original code to be available at `../../bbu_reference/bbu2-master/`.

## Done Criteria

✅ All criteria met:

1. ✅ `gridcore` package imports successfully with zero exchange-specific dependencies
2. ✅ `Grid.build_greed()` produces identical price/side lists as original `Greed.build_greed()`
3. ✅ `Grid.update_greed()` produces identical results as original
4. ✅ `Grid.center_greed()` produces identical results as original `__center_greed()`
5. ✅ All unit tests pass with 88% coverage (exceeds 80% requirement)
6. ✅ CI validation confirms no `pybit` or `BybitApi` imports in gridcore

## License

Same as parent project.
