# Phase E: Test Plan

## Overview

This document outlines the testing strategy for validating the gridbot implementation before committing.

## 1. Unit Tests (Automated)

### Status: COMPLETE (101 tests passing)

```bash
uv run pytest apps/gridbot/tests -v
```

| Module | Tests | Coverage |
|--------|-------|----------|
| config.py | 16 | Config loading, validation, YAML parsing |
| executor.py | 19 | Place/cancel execution, shadow mode, batch |
| retry_queue.py | 19 | Backoff, max attempts, background processing |
| runner.py | 23 | Order tracking, event handling, position updates |
| reconciler.py | 15 | Startup sync, reconnect, orphan detection |
| orchestrator.py | 9 | Initialization, routing, lifecycle |

---

## 2. Integration Tests

### 2.1 Config Loading Test

**Test**: Verify config loads correctly from YAML file.

```bash
# Create test config
cat > /tmp/test_gridbot.yaml << 'EOF'
accounts:
  - name: "test_account"
    api_key: "test_key"
    api_secret: "test_secret"
    testnet: true

strategies:
  - strat_id: "btcusdt_test"
    account: "test_account"
    symbol: "BTCUSDT"
    tick_size: "0.1"
    grid_count: 20
    grid_step: 0.2
    shadow_mode: true

database_url: "sqlite:///test.db"
EOF

# Test loading
uv run python -c "
from gridbot.config import load_config
config = load_config('/tmp/test_gridbot.yaml')
print(f'Accounts: {len(config.accounts)}')
print(f'Strategies: {len(config.strategies)}')
print(f'Strategy: {config.strategies[0].strat_id}')
print(f'Shadow mode: {config.strategies[0].shadow_mode}')
"
```

**Expected Output**:
```
Accounts: 1
Strategies: 1
Strategy: btcusdt_test
Shadow mode: True
```

### 2.2 Executor Shadow Mode Test

**Test**: Verify shadow mode logs but doesn't execute.

```bash
uv run python -c "
from decimal import Decimal
from unittest.mock import Mock
from gridcore.intents import PlaceLimitIntent
from gridbot.executor import IntentExecutor

# Create mock REST client
mock_client = Mock()
mock_client.place_order = Mock()

# Create shadow executor
executor = IntentExecutor(mock_client, shadow_mode=True)

# Create intent
intent = PlaceLimitIntent.create(
    symbol='BTCUSDT',
    side='Buy',
    price=Decimal('50000'),
    qty=Decimal('0.001'),
    grid_level=10,
    direction='long',
)

# Execute
result = executor.execute_place(intent)

print(f'Success: {result.success}')
print(f'Order ID starts with shadow_: {result.order_id.startswith(\"shadow_\")}')
print(f'REST client called: {mock_client.place_order.called}')
"
```

**Expected Output**:
```
Success: True
Order ID starts with shadow_: True
REST client called: False
```

### 2.3 Retry Queue Test

**Test**: Verify retry queue handles failures correctly.

```bash
uv run python -c "
import asyncio
from unittest.mock import Mock
from gridbot.retry_queue import RetryQueue
from gridcore.intents import PlaceLimitIntent
from decimal import Decimal

async def test():
    # Track calls
    call_count = [0]

    def executor(intent):
        call_count[0] += 1
        result = Mock()
        result.success = call_count[0] >= 2  # Succeed on 2nd try
        result.error = 'Simulated error' if not result.success else None
        return result

    queue = RetryQueue(
        executor_func=executor,
        max_attempts=3,
        initial_backoff_seconds=0.01,
    )

    intent = PlaceLimitIntent.create(
        symbol='BTCUSDT',
        side='Buy',
        price=Decimal('50000'),
        qty=Decimal('0.001'),
        grid_level=10,
        direction='long',
    )

    queue.add(intent, 'Initial error')
    print(f'Queue size after add: {queue.size}')

    # Process (first retry - should fail)
    await asyncio.sleep(0.02)
    await queue.process_due()
    print(f'Queue size after 1st process: {queue.size}')

    # Process (second retry - should succeed)
    await asyncio.sleep(0.05)
    await queue.process_due()
    print(f'Queue size after 2nd process: {queue.size}')
    print(f'Total executor calls: {call_count[0]}')

asyncio.run(test())
"
```

**Expected Output**:
```
Queue size after add: 1
Queue size after 1st process: 1
Queue size after 2nd process: 0
Total executor calls: 2
```

### 2.4 Runner Grid Building Test

**Test**: Verify runner builds grid on first ticker event.

```bash
uv run python -c "
import asyncio
from decimal import Decimal
from datetime import datetime, UTC
from unittest.mock import Mock, MagicMock
from gridcore import TickerEvent, EventType
from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor, OrderResult
from gridbot.runner import StrategyRunner

async def test():
    # Config
    config = StrategyConfig(
        strat_id='test',
        account='test',
        symbol='BTCUSDT',
        tick_size=Decimal('0.1'),
        grid_count=20,
        grid_step=0.2,
        shadow_mode=True,
    )

    # Mock executor
    executor = Mock(spec=IntentExecutor)
    executor.shadow_mode = True
    executor.execute_place = MagicMock(return_value=OrderResult(success=True, order_id='shadow_123'))
    executor.execute_cancel = MagicMock()

    # Create runner
    runner = StrategyRunner(config, executor)

    print(f'Grid size before ticker: {len(runner.engine.grid.grid)}')

    # Send ticker event
    ticker = TickerEvent(
        event_type=EventType.TICKER,
        symbol='BTCUSDT',
        exchange_ts=datetime.now(UTC),
        local_ts=datetime.now(UTC),
        last_price=Decimal('50000'),
        mark_price=Decimal('50000'),
        bid1_price=Decimal('49999'),
        ask1_price=Decimal('50001'),
        funding_rate=Decimal('0.0001'),
    )

    intents = await runner.on_ticker(ticker)

    print(f'Grid size after ticker: {len(runner.engine.grid.grid)}')
    print(f'Intents generated: {len(intents)}')
    print(f'Last close: {runner.engine.last_close}')

asyncio.run(test())
"
```

**Expected Output**:
```
Grid size before ticker: 0
Grid size after ticker: 20
Intents generated: [some number > 0]
Last close: 50000.0
```

### 2.5 Reconciler Test

**Test**: Verify reconciler identifies our orders vs orphans.

```bash
uv run python -c "
import asyncio
from decimal import Decimal
from unittest.mock import Mock, MagicMock
from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor, OrderResult
from gridbot.runner import StrategyRunner
from gridbot.reconciler import Reconciler

async def test():
    # Mock REST client
    mock_client = Mock()
    mock_client.get_open_orders = MagicMock(return_value=[
        {'orderId': 'ex_1', 'orderLinkId': 'abc123def456789a'},  # Our order (16 hex)
        {'orderId': 'ex_2', 'orderLinkId': '1234567890abcdef'},  # Our order (16 hex)
        {'orderId': 'ex_3', 'orderLinkId': 'manual_order'},      # Orphan
        {'orderId': 'ex_4', 'orderLinkId': ''},                  # Orphan (empty)
    ])

    # Create reconciler
    reconciler = Reconciler(mock_client)

    # Create runner
    config = StrategyConfig(
        strat_id='test',
        account='test',
        symbol='BTCUSDT',
        tick_size=Decimal('0.1'),
        shadow_mode=True,
    )
    executor = Mock(spec=IntentExecutor)
    executor.shadow_mode = True
    runner = StrategyRunner(config, executor)

    # Run reconciliation
    result = await reconciler.reconcile_startup(runner)

    print(f'Orders fetched: {result.orders_fetched}')
    print(f'Orders injected (ours): {result.orders_injected}')
    print(f'Orphan orders: {result.orphan_orders}')
    print(f'Tracked orders in runner: {runner.get_tracked_order_count()}')

asyncio.run(test())
"
```

**Expected Output**:
```
Orders fetched: 4
Orders injected (ours): 2
Orphan orders: 2
Tracked orders in runner: {'pending': 0, 'placed': 2, 'filled': 0, 'cancelled': 0, 'failed': 0}
```

---

## 3. Manual Testing with Testnet

### Prerequisites

1. Bybit testnet account with API keys
2. Test USDT balance on testnet
3. Create config file:

```bash
cp apps/gridbot/conf/gridbot.yaml.example apps/gridbot/conf/gridbot.yaml
# Edit with your testnet credentials
```

### 3.1 Shadow Mode Test (No Real Orders)

**Purpose**: Verify strategy logic without placing real orders.

```yaml
# conf/gridbot.yaml
accounts:
  - name: "testnet"
    api_key: "YOUR_TESTNET_KEY"
    api_secret: "YOUR_TESTNET_SECRET"
    testnet: true

strategies:
  - strat_id: "btcusdt_shadow"
    account: "testnet"
    symbol: "BTCUSDT"
    tick_size: "0.1"
    grid_count: 10
    grid_step: 0.5
    shadow_mode: true  # <-- IMPORTANT: No real orders
```

```bash
# Run in shadow mode
uv run python -m gridbot.main --config apps/gridbot/conf/gridbot.yaml --debug

# Expected: See log messages like:
# [SHADOW] Would place Buy order: BTCUSDT qty=... price=...
# [SHADOW] Would place Sell order: BTCUSDT qty=... price=...
```

**Verification**:
- [ ] Bot starts without errors
- [ ] Grid is built (see log: "Building grid from market price")
- [ ] Shadow intents are logged
- [ ] No orders appear on Bybit testnet
- [ ] Ctrl+C gracefully shuts down

### 3.2 Live Mode Test (Real Testnet Orders)

**Purpose**: Verify actual order placement on testnet.

**WARNING**: This will place real orders on testnet!

```yaml
# conf/gridbot.yaml
strategies:
  - strat_id: "btcusdt_live"
    account: "testnet"
    symbol: "BTCUSDT"
    tick_size: "0.1"
    grid_count: 6        # Small grid for testing
    grid_step: 1.0       # Wide spacing
    amount: "x0.0001"    # Tiny amount
    shadow_mode: false   # <-- Live mode
```

```bash
# Run live
uv run python -m gridbot.main --config apps/gridbot/conf/gridbot.yaml --debug
```

**Verification**:
- [ ] Bot starts without errors
- [ ] Orders appear on Bybit testnet
- [ ] Orders are at correct grid prices
- [ ] Position check runs (~63s interval)
- [ ] Ctrl+C cancels orders or leaves them (check behavior)

### 3.3 Restart Test

**Purpose**: Verify grid anchor persistence across restarts.

```bash
# Start bot, let it build grid
uv run python -m gridbot.main --config apps/gridbot/conf/gridbot.yaml

# Wait for grid to build, note the grid prices
# Ctrl+C to stop

# Check anchor file
cat db/grid_anchor.json

# Restart bot
uv run python -m gridbot.main --config apps/gridbot/conf/gridbot.yaml

# Expected: "Building grid from anchor price" in logs
```

**Verification**:
- [ ] Anchor file created after first run
- [ ] Second run uses anchor price
- [ ] Grid levels match between runs

---

## 4. Error Handling Tests

### 4.1 Invalid Config Test

```bash
# Test missing account reference
cat > /tmp/bad_config.yaml << 'EOF'
accounts:
  - name: "real_account"
    api_key: "key"
    api_secret: "secret"
    testnet: true

strategies:
  - strat_id: "test"
    account: "nonexistent_account"  # <-- Bad reference
    symbol: "BTCUSDT"
    tick_size: "0.1"
EOF

uv run python -m gridbot.main --config /tmp/bad_config.yaml
# Expected: Error about unknown account
```

### 4.2 Network Error Simulation

```bash
# Start bot with invalid credentials (will fail to connect)
cat > /tmp/bad_creds.yaml << 'EOF'
accounts:
  - name: "bad"
    api_key: "invalid_key"
    api_secret: "invalid_secret"
    testnet: true

strategies:
  - strat_id: "test"
    account: "bad"
    symbol: "BTCUSDT"
    tick_size: "0.1"
    shadow_mode: true
EOF

uv run python -m gridbot.main --config /tmp/bad_creds.yaml
# Expected: Connection/auth errors in logs, but bot should handle gracefully
```

---

## 5. Test Checklist

### Unit Tests
- [ ] `uv run pytest apps/gridbot/tests -v` - All 101 tests pass

### Integration Tests
- [ ] 2.1 Config loading works
- [ ] 2.2 Shadow mode doesn't call REST
- [ ] 2.3 Retry queue retries correctly
- [ ] 2.4 Runner builds grid
- [ ] 2.5 Reconciler identifies orders

### Manual Tests (Shadow Mode)
- [ ] 3.1 Bot starts in shadow mode
- [ ] 3.1 Grid builds correctly
- [ ] 3.1 Intents logged but not executed
- [ ] 3.1 Graceful shutdown

### Manual Tests (Live Testnet) - OPTIONAL
- [ ] 3.2 Orders placed on testnet
- [ ] 3.2 Orders at correct prices
- [ ] 3.3 Anchor persistence works

### Error Handling
- [ ] 4.1 Invalid config rejected
- [ ] 4.2 Network errors handled

---

## 6. Running the Tests

### Quick Validation (Automated Only)

```bash
# Run all unit tests
uv run pytest apps/gridbot/tests -v

# Run integration tests
bash -c '
echo "=== 2.1 Config Loading ==="
# [paste test 2.1 code]

echo "=== 2.2 Shadow Mode ==="
# [paste test 2.2 code]

# ... etc
'
```

### Full Validation (Including Manual)

1. Run automated tests
2. Create testnet config
3. Run shadow mode test
4. (Optional) Run live testnet test
5. Verify error handling

---

## 7. Known Limitations

1. **Database Integration**: Run records not fully implemented (logged only)
2. **WebSocket Reconnection**: Relies on pybit's built-in reconnection
3. **Multi-Account**: Tested with single account only
4. **Amount Calculation**: Uses fixed amount, wallet fraction not implemented

These are acceptable for Phase E MVP and can be enhanced in future iterations.
