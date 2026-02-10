#!/usr/bin/env python3
"""Interactive walkthrough of backtest implementation.

Run with: uv run python debug_walkthrough.py

This script demonstrates the complete data flow through the backtest system,
printing state at each step to help understand the implementation.

ARCHITECTURE OVERVIEW
=====================

The backtest system simulates grid trading without connecting to an exchange.
It processes historical price data tick-by-tick and simulates order fills.

Key data flow:

  Historical Data (ticks)
         │
         ▼
  ┌─────────────────┐
  │  BacktestEngine │  ← Orchestrates everything
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │ BacktestRunner  │  ← Wraps GridEngine, processes ticks
  └────────┬────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
┌─────────┐ ┌──────────┐
│GridEngine│ │ Executor │  ← Strategy logic │ Order placement
└────┬────┘ └────┬─────┘
     │           │
     ▼           ▼
  Intents → BacktestOrderManager → Fills → PositionTracker → PnL
                                              │
                                              ▼
                                      BacktestSession
                                    (equity, metrics)

Components (bottom-up):
1. FillSimulator     - Decides when limit orders fill (trade-through model)
2. PositionTracker   - Tracks position size, entry price, calculates PnL
3. OrderManager      - Manages order lifecycle (place, fill, cancel)
4. Executor          - Converts intents to orders
5. Runner            - Wraps GridEngine, two-phase tick processing
6. Engine            - Orchestrates multiple strategies, funding, wind-down
7. Session           - Stores results, calculates metrics
8. Reporter          - Exports to CSV
"""

from datetime import datetime, timedelta
from decimal import Decimal

# ============================================================================
# SECTION 1: Fill Simulator - How orders get filled
# ============================================================================
print("\n" + "=" * 70)
print("SECTION 1: FILL SIMULATOR")
print("=" * 70)

print("""
WHAT: TradeThroughFillSimulator determines when a limit order should fill.

WHY: In backtesting, we don't have a real order book. We need a model to
     decide when orders would have filled based on price movement.

HOW: "Strict cross" model - an order fills when the market price CROSSES
     through the limit price (not just touches it):
     - BUY order fills when current_price < limit_price
       (price dropped BELOW where we wanted to buy)
     - SELL order fills when current_price > limit_price
       (price rose ABOVE where we wanted to sell)
     - At limit price (price == limit), order does NOT fill

WHY STRICT CROSS (not touch)?
     - At limit price, fill is not guaranteed (queue position, volume)
     - Conservative assumption is better for backtesting
     - Grid orders sit at common price levels with competition

FILE: src/backtest/fill_simulator.py
      - _should_fill(side, limit_price, current_price) -> bool
      - get_fill_price(side, limit_price, current_price) -> Decimal

NOTE: Fill price = limit price. In reality, you might get better fills,
      but assuming limit price is safer for backtesting.
""")

from backtest.fill_simulator import TradeThroughFillSimulator

simulator = TradeThroughFillSimulator()

# Strict cross model: order fills when price CROSSES the limit (not just touches)
print("\nStrict cross fill model:")
print("  - BUY fills when current_price < limit_price (price must go BELOW)")
print("  - SELL fills when current_price > limit_price (price must go ABOVE)")
print("  - At limit price (==), NO fill (queue position unknown)")

# Test cases
test_cases = [
    ("Buy", Decimal("100"), Decimal("99"), "price below limit"),
    ("Buy", Decimal("100"), Decimal("100"), "price at limit"),
    ("Buy", Decimal("100"), Decimal("101"), "price above limit"),
    ("Sell", Decimal("100"), Decimal("101"), "price above limit"),
    ("Sell", Decimal("100"), Decimal("100"), "price at limit"),
    ("Sell", Decimal("100"), Decimal("99"), "price below limit"),
]

print("\nFill simulation results:")
for side, limit, current, desc in test_cases:
    # Access internal method for demo (normally called via check_fills)
    should_fill = simulator._should_fill(side, limit, current)
    symbol = "✓" if should_fill else "✗"
    print(f"  {side:4} @ {limit} when price={current} ({desc}): {symbol} {'FILLS' if should_fill else 'no fill'}")


# ============================================================================
# SECTION 2: Position Tracker - PnL Calculations
# ============================================================================
print("\n" + "=" * 70)
print("SECTION 2: POSITION TRACKER")
print("=" * 70)

print("""
WHAT: BacktestPositionTracker tracks position state and calculates PnL.

WHY: We need to know our position size, entry price, and profit/loss at
     any point during the backtest. This is separate from gridcore.Position
     which handles risk multipliers (different purpose).

HOW: Maintains PositionState dataclass with:
     - size: Current position size (0 = no position)
     - entry_price: Weighted average entry price
     - realized_pnl: PnL from closed trades
     - unrealized_pnl: Paper PnL on open position (USDT)
     - unrealized_pnl_percent: Paper PnL as ROE % (for risk management)
     - commission_paid: Total fees paid
     - funding_paid: Total funding payments

KEY METHODS:
     - process_fill(side, qty, price) -> realized_pnl
       Called when an order fills. Updates position, returns realized PnL.

     - calculate_unrealized_pnl(current_price) -> Decimal
       Calculates paper profit on open position at given price (USDT).

     - calculate_unrealized_pnl_percent(current_price, leverage) -> Decimal
       Calculates ROE % for risk management (bbu2 formula).

     - apply_funding(rate, price) -> funding_amount
       Applies 8-hour funding payment to position.

PnL FORMULAS:
     Long:  (exit_price - entry_price) * size
     Short: (entry_price - exit_price) * size

ROE % FORMULA (from bbu2):
     Long:  (1/entry - 1/close) * entry * 100 * leverage
     Short: (1/close - 1/entry) * entry * 100 * leverage

FILE: src/backtest/position_tracker.py

NOTE: Each direction (long/short) has its own tracker. The backtest uses
      TWO trackers per strategy, just like Bybit's hedge mode.
""")

from backtest.position_tracker import BacktestPositionTracker

tracker = BacktestPositionTracker(direction="long", commission_rate=Decimal("0.0006"))

print("\nLong position lifecycle:")
print(f"  Initial state: size={tracker.state.size}, entry={tracker.state.avg_entry_price}")

# Open position
realized = tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("50000"))
print(f"\n  After BUY 0.1 @ 50000:")
print(f"    size={tracker.state.size}, entry_price={tracker.state.avg_entry_price}")
print(f"    realized_pnl={realized} (opening trade, no PnL yet)")
print(f"    commission_paid={tracker.state.commission_paid}")

# Check unrealized PnL at different prices
for price in [49000, 50000, 51000]:
    unrealized = tracker.calculate_unrealized_pnl(Decimal(str(price)))
    print(f"\n  Unrealized PnL @ {price}: {unrealized}")
    print(f"    Formula: (current - entry) * size = ({price} - 50000) * 0.1 = {unrealized}")

# Check unrealized PnL % (ROE) - used for risk management
print("\n  Unrealized PnL % (ROE) at different prices (leverage=10x):")
leverage = Decimal("10")
for price in [49000, 50000, 51000]:
    pnl_percent = tracker.calculate_unrealized_pnl_percent(Decimal(str(price)), leverage)
    print(f"    @ {price}: {pnl_percent:.2f}%")
    print(f"      Formula: (1/entry - 1/close) * entry * 100 * leverage")

# Close position
realized = tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("51000"))
print(f"\n  After SELL 0.1 @ 51000 (close):")
print(f"    size={tracker.state.size} (position closed)")
print(f"    realized_pnl={realized}")
print(f"    Formula: (exit - entry) * size = (51000 - 50000) * 0.1 = {realized}")
print(f"    total_realized_pnl={tracker.state.realized_pnl}")
print(f"    total_commission={tracker.state.commission_paid}")


# ============================================================================
# SECTION 3: Order Manager - Order Lifecycle
# ============================================================================
print("\n" + "=" * 70)
print("SECTION 3: ORDER MANAGER")
print("=" * 70)

print("""
WHAT: BacktestOrderManager simulates an exchange's order book.

WHY: GridEngine produces PlaceLimitIntent/CancelIntent. We need something
     to "execute" these intents and track order state without a real exchange.

HOW: Maintains three collections:
     - active_orders: Dict[order_id, SimulatedOrder] - open orders
     - filled_orders: List[SimulatedOrder] - filled orders (for lookup)
     - cancelled_orders: List[SimulatedOrder] - cancelled orders

ORDER LIFECYCLE:
     1. place_order() -> Creates SimulatedOrder, adds to active_orders
     2. check_fills() -> On each tick, checks if any active orders should fill
        - Uses FillSimulator to decide
        - Moves filled orders to filled_orders list
        - Returns ExecutionEvent for each fill
     3. cancel_order() -> Moves order to cancelled_orders

KEY METHODS:
     - place_order(symbol, side, price, qty, direction, client_order_id, timestamp)
       Creates and tracks a new limit order.

     - check_fills(current_price, timestamp, symbol) -> List[ExecutionEvent]
       Called every tick. Returns events for orders that filled.

     - cancel_order(order_id) -> bool
       Cancels an active order.

     - get_limit_orders() -> Dict with 'long' and 'short' lists
       Returns active orders in GridEngine-expected format.

     - get_order_by_client_id(client_order_id) -> SimulatedOrder
       Looks up order by client ID (searches both active AND filled).

FILE: src/backtest/order_manager.py

NOTE: client_order_id tracking prevents duplicate orders. IDs are released
      on fill/cancel so they can be reused (important for grid strategies).
""")

from backtest.order_manager import BacktestOrderManager

order_mgr = BacktestOrderManager(
    fill_simulator=TradeThroughFillSimulator(),
    commission_rate=Decimal("0.0006"),
)

print("\nOrder lifecycle:")

# Place order
order = order_mgr.place_order(
    client_order_id="test_order_001",
    symbol="BTCUSDT",
    side="Buy",
    price=Decimal("50000"),
    qty=Decimal("0.1"),
    direction="long",
    grid_level=5,
    timestamp=datetime(2025, 1, 1, 10, 0),
)
print(f"\n  Placed order: {order.client_order_id}")
print(f"    order_id={order.order_id}")
print(f"    side={order.side}, price={order.price}, qty={order.qty}")
print(f"    status={order.status}")
print(f"    Active orders: {len(order_mgr.active_orders)}")

# Check fills at different prices
print("\n  Checking fills at different prices:")
for price in [50100, 50000, 49900]:
    # Reset for demo (normally you wouldn't do this)
    if order.order_id not in order_mgr.active_orders:
        order_mgr.active_orders[order.order_id] = order
        order.status = "New"

    fills = order_mgr.check_fills(
        current_price=Decimal(str(price)),
        timestamp=datetime(2025, 1, 1, 10, 1),
        symbol="BTCUSDT",
    )
    if fills:
        print(f"    Price {price}: FILLED! ExecutionEvent generated")
        print(f"      exec_id={fills[0].exec_id}")
        print(f"      price={fills[0].price}, qty={fills[0].qty}, fee={fills[0].fee}")
    else:
        print(f"    Price {price}: No fill (price > limit)")


# ============================================================================
# SECTION 4: Session - Equity Curve & Metrics
# ============================================================================
print("\n" + "=" * 70)
print("SECTION 4: SESSION - EQUITY & METRICS")
print("=" * 70)

print("""
WHAT: BacktestSession stores all results and calculates final metrics.

WHY: We need to track:
     - Every trade that occurred
     - Equity at each point in time (for drawdown, Sharpe)
     - Running totals (realized PnL, commissions, funding)
     - Final performance metrics

HOW: Accumulates data during backtest, then finalize() computes metrics.

DATA STORED:
     - trades: List[BacktestTrade] - all executed trades
     - equity_curve: List[(timestamp, equity)] - equity over time
     - total_realized_pnl, total_commission, total_funding - running totals
     - total_volume - for turnover calculation

KEY METHODS:
     - record_trade(trade) - Called when a fill occurs
     - record_funding(amount) - Called at funding times
     - update_equity(timestamp, unrealized_pnl) -> equity
       Called every tick. Updates equity curve and drawdown tracking.
     - finalize(final_unrealized_pnl) -> BacktestMetrics
       Calculates all final metrics.

EQUITY FORMULA:
     equity = initial_balance + realized_pnl + unrealized_pnl - commission + funding

METRICS CALCULATED (BacktestMetrics dataclass):
     Trade stats:    total_trades, winning/losing, win_rate, avg_win/loss
     PnL:            realized, unrealized, commission, funding, net
     Risk:           max_drawdown ($ and %), drawdown_duration, sharpe_ratio
     Balance:        initial, final, return_pct
     Activity:       total_volume, turnover (volume / capital)
     Direction:      long/short trades, pnl, profit_factor

FILE: src/backtest/session.py

NOTE: Drawdown duration counts ticks (not time). Sharpe ratio assumes
      minute-level data by default (252 * 24 * 60 periods/year).
""")

from backtest.session import BacktestSession, BacktestTrade

session = BacktestSession(initial_balance=Decimal("10000"))

print(f"\nSession initialized:")
print(f"  session_id={session.session_id[:8]}...")
print(f"  initial_balance={session.initial_balance}")
print(f"  current_balance={session.current_balance}")

# Record some trades
trades = [
    BacktestTrade(
        trade_id="t1", symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
        qty=Decimal("0.1"), direction="long", timestamp=datetime(2025, 1, 1, 10, 0),
        order_id="o1", client_order_id="c1", realized_pnl=Decimal("0"),
        commission=Decimal("3"), strat_id="strat1",
    ),
    BacktestTrade(
        trade_id="t2", symbol="BTCUSDT", side="Sell", price=Decimal("51000"),
        qty=Decimal("0.1"), direction="long", timestamp=datetime(2025, 1, 1, 12, 0),
        order_id="o2", client_order_id="c2", realized_pnl=Decimal("100"),
        commission=Decimal("3.06"), strat_id="strat1",
    ),
]

print("\nRecording trades:")
for trade in trades:
    session.record_trade(trade)
    print(f"  {trade.side} {trade.qty} @ {trade.price}: realized_pnl={trade.realized_pnl}")

print(f"\nAfter trades:")
print(f"  total_realized_pnl={session.total_realized_pnl}")
print(f"  total_commission={session.total_commission}")
print(f"  total_volume={session.total_volume}")

# Update equity curve
print("\nEquity curve updates:")
timestamps = [
    (datetime(2025, 1, 1, 10, 0), Decimal("0")),      # Just opened
    (datetime(2025, 1, 1, 11, 0), Decimal("50")),     # Unrealized gain
    (datetime(2025, 1, 1, 11, 30), Decimal("-20")),   # Drawdown
    (datetime(2025, 1, 1, 12, 0), Decimal("0")),      # Closed position
]

for ts, unrealized in timestamps:
    equity = session.update_equity(ts, unrealized)
    print(f"  {ts.strftime('%H:%M')}: unrealized={unrealized:>6}, equity={equity:.2f}")

print(f"\nDrawdown tracking:")
print(f"  peak_equity={session._peak_equity}")
print(f"  max_drawdown={session._max_drawdown}")
print(f"  current_drawdown_duration={session._current_drawdown_duration} ticks")

# Finalize metrics
print("\nFinalizing metrics...")
metrics = session.finalize(final_unrealized_pnl=Decimal("0"))

print(f"\nBacktestMetrics:")
print(f"  total_trades={metrics.total_trades}")
print(f"  winning_trades={metrics.winning_trades}, losing_trades={metrics.losing_trades}")
print(f"  win_rate={metrics.win_rate:.1%}")
print(f"  profit_factor={metrics.profit_factor:.2f}")
print(f"  net_pnl={metrics.net_pnl}")
print(f"  return_pct={metrics.return_pct:.2f}%")
print(f"  max_drawdown={metrics.max_drawdown} ({metrics.max_drawdown_pct:.1f}%)")
print(f"  max_drawdown_duration={metrics.max_drawdown_duration} ticks")
print(f"  sharpe_ratio={metrics.sharpe_ratio:.2f}")
print(f"  turnover={metrics.turnover:.2f}x")


# ============================================================================
# SECTION 5: Runner - Two-Phase Tick Processing
# ============================================================================
print("\n" + "=" * 70)
print("SECTION 5: RUNNER - TWO-PHASE TICK PROCESSING")
print("=" * 70)

print("""
WHAT: BacktestRunner wraps GridEngine and handles tick processing.

WHY: GridEngine is pure strategy logic - it doesn't know about backtesting.
     Runner bridges GridEngine with the backtest infrastructure:
     - Feeds ticks to GridEngine
     - Executes intents via Executor
     - Processes fills and updates positions
     - Tracks PnL via PositionTrackers

HOW: TWO-PHASE tick processing (critical for correct equity timing):

     Phase 1: process_fills(tick)
     ├── Check if any orders filled at current price
     ├── For each fill:
     │   ├── Update PositionTracker (realized PnL)
     │   ├── Record trade in Session
     │   └── Notify GridEngine (ExecutionEvent)
     └── Return intents from fills (if any)

     [Engine updates equity HERE - after fills, before new orders]

     Phase 2: execute_tick(tick)
     ├── Pass TickerEvent to GridEngine
     ├── GridEngine returns intents (PlaceLimitIntent, CancelIntent)
     └── Execute intents via Executor (place/cancel orders)

WHY TWO PHASES?
     - Fills from current tick must be reflected in equity BEFORE
       placing new orders (wallet-fraction sizing uses current balance)
     - If we did everything in one phase, equity would lag by one tick

KEY METHODS:
     - process_fills(event) -> List[Intent]
       Phase 1: Check fills, update positions, record trades

     - execute_tick(event) -> List[Intent]
       Phase 2: Get intents from engine, execute them

     - process_tick(event) -> List[Intent]
       Legacy single-phase method (calls both, for backward compatibility)

     - apply_funding(rate, price) -> total_funding
       Apply funding to both long and short positions

COMPONENTS OWNED:
     - _engine: GridEngine instance
     - _executor: BacktestExecutor
     - _long_tracker, _short_tracker: PositionTrackers
     - _session: BacktestSession reference

FILE: src/backtest/runner.py

NOTE: Runner does NOT update equity - that's done by Engine (Section 6)
      to support multi-strategy aggregation.
""")

from backtest.config import BacktestStrategyConfig
from backtest.executor import BacktestExecutor
from backtest.runner import BacktestRunner
from gridcore import TickerEvent, EventType

# Create components
strategy_config = BacktestStrategyConfig(
    strat_id="demo_strat",
    symbol="BTCUSDT",
    tick_size=Decimal("0.1"),
    grid_count=10,
    grid_step=0.5,
    amount="100",  # Fixed $100 per order
    commission_rate=Decimal("0.0006"),
)

fill_sim = TradeThroughFillSimulator()
order_mgr = BacktestOrderManager(fill_simulator=fill_sim, commission_rate=Decimal("0.0006"))
executor = BacktestExecutor(order_manager=order_mgr)
session = BacktestSession(initial_balance=Decimal("10000"))

runner = BacktestRunner(
    strategy_config=strategy_config,
    executor=executor,
    session=session,
)

print("\nRunner created:")
print(f"  strat_id={runner.strat_id}")
print(f"  symbol={runner.symbol}")

# First tick - builds grid
tick1_ts = datetime(2025, 1, 1, 10, 0)
tick1 = TickerEvent(
    event_type=EventType.TICKER,
    symbol="BTCUSDT",
    last_price=Decimal("50000"),
    exchange_ts=tick1_ts,
    local_ts=tick1_ts,
)

print(f"\n--- Tick 1: price={tick1.last_price} ---")
print("  Phase 1: process_fills()")
fill_intents = runner.process_fills(tick1)
print(f"    fills processed: {len(fill_intents)} intents from fills")

print("  [Engine would update equity here]")

print("  Phase 2: execute_tick()")
tick_intents = runner.execute_tick(tick1)
print(f"    intents generated: {len(tick_intents)}")

# Show grid state
if runner.engine.grid.grid:
    print(f"\n  Grid built with {len(runner.engine.grid.grid)} levels:")
    for i, level in enumerate(runner.engine.grid.grid[:3]):
        print(f"    [{i}] price={level['price']}, side={level['side']}")
    print("    ...")
    for i, level in enumerate(runner.engine.grid.grid[-3:], len(runner.engine.grid.grid) - 3):
        print(f"    [{i}] price={level['price']}, side={level['side']}")

# Show placed orders
limit_orders = order_mgr.get_limit_orders()
print(f"\n  Orders placed:")
print(f"    Long orders: {len(limit_orders.get('long', []))}")
print(f"    Short orders: {len(limit_orders.get('short', []))}")

if limit_orders.get('long'):
    print(f"\n  Sample long order:")
    sample = limit_orders['long'][0]
    print(f"    price={sample['price']}, side={sample['side']}")


# ============================================================================
# SECTION 6: Engine - Full Orchestration
# ============================================================================
print("\n" + "=" * 70)
print("SECTION 6: ENGINE - FULL ORCHESTRATION")
print("=" * 70)

print("""
WHAT: BacktestEngine is the main orchestrator that runs backtests.

WHY: Coordinates all the pieces:
     - Creates runners for each strategy
     - Feeds data from provider
     - Handles funding simulation
     - Manages wind-down at end
     - Finalizes session metrics

HOW: Main loop in run():

     for tick in data_provider:
         _process_tick(tick)
             │
             ├── 1. Apply funding (if 00:00, 08:00, or 16:00 UTC)
             │      └── FundingSimulator checks time
             │      └── runner.apply_funding() for each runner
             │
             ├── 2. Phase 1: Process fills for ALL runners
             │      └── runner.process_fills(tick)
             │      └── Updates realized PnL in session
             │
             ├── 3. Update equity (AFTER fills, BEFORE new orders)
             │      └── Aggregate unrealized PnL from ALL runners
             │      └── session.update_equity(timestamp, total_unrealized)
             │
             └── 4. Phase 2: Execute tick for ALL runners
                    └── runner.execute_tick(tick)
                    └── Uses updated balance for sizing

     After loop:
         _wind_down()  # Close positions if wind_down_mode="close_all"
         session.finalize()

MULTI-STRATEGY SUPPORT:
     - Multiple runners can trade same symbol
     - Equity aggregates unrealized PnL from ALL runners
     - Each runner has independent positions

WIND-DOWN MODES:
     - "leave_open": Positions stay open, unrealized PnL in metrics
     - "close_all": Force-close all positions at last price

FUNDING SIMULATION:
     - Bybit funding every 8 hours (00:00, 08:00, 16:00 UTC)
     - Long pays, short receives when rate > 0
     - Configurable via enable_funding and funding_rate

KEY METHODS:
     - run(symbol, start_ts, end_ts, data_provider) -> BacktestSession
       Main entry point. Runs backtest, returns session with results.

     - _process_tick(tick) - Handles one tick (see flow above)
     - _wind_down() - End-of-backtest position handling
     - _force_close_position() - Close position at given price

FILE: src/backtest/engine.py

NOTE: Engine resets state at start of run() to support multiple runs.
      _runners, _last_prices, _funding_simulator all get cleared.
""")

from backtest.config import BacktestConfig
from backtest.engine import BacktestEngine, FundingSimulator
from backtest.data_provider import InMemoryDataProvider

# Create config
config = BacktestConfig(
    initial_balance=Decimal("10000"),
    enable_funding=True,
    funding_rate=Decimal("0.0001"),
    wind_down_mode="close_all",
    strategies=[
        BacktestStrategyConfig(
            strat_id="btc_grid",
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            grid_count=10,
            grid_step=0.5,
            amount="100",
            commission_rate=Decimal("0.0006"),
        ),
    ],
)

print("\nEngine config:")
print(f"  initial_balance={config.initial_balance}")
print(f"  enable_funding={config.enable_funding}")
print(f"  funding_rate={config.funding_rate}")
print(f"  wind_down_mode={config.wind_down_mode}")
print(f"  strategies: {[s.strat_id for s in config.strategies]}")

# Create test data
base_time = datetime(2025, 1, 1, 0, 0)  # Start at funding time
ticks = []
prices = [50000, 49900, 49800, 49700, 49800, 49900, 50000, 50100, 50200]

for i, price in enumerate(prices):
    tick_ts = base_time + timedelta(minutes=i)
    ticks.append(TickerEvent(
        event_type=EventType.TICKER,
        symbol="BTCUSDT",
        last_price=Decimal(str(price)),
        exchange_ts=tick_ts,
        local_ts=tick_ts,
    ))

data_provider = InMemoryDataProvider(ticks)

print(f"\nTest data: {len(ticks)} ticks")
print(f"  Price range: {min(prices)} - {max(prices)}")
print(f"  Time range: {ticks[0].exchange_ts} - {ticks[-1].exchange_ts}")

# Run backtest
print("\nRunning backtest...")
engine = BacktestEngine(config=config)
session = engine.run(
    symbol="BTCUSDT",
    start_ts=base_time,
    end_ts=base_time + timedelta(hours=1),
    data_provider=data_provider,
)

print(f"\nBacktest complete!")
print(f"  Trades executed: {len(session.trades)}")
print(f"  Equity points: {len(session.equity_curve)}")

if session.trades:
    print(f"\n  Sample trades:")
    for trade in session.trades[:5]:
        print(f"    {trade.side} {trade.qty} @ {trade.price} -> pnl={trade.realized_pnl}")

print(f"\nFinal metrics:")
m = session.metrics
print(f"  total_trades={m.total_trades}")
print(f"  net_pnl={m.net_pnl}")
print(f"  return_pct={m.return_pct:.2f}%")
print(f"  final_balance={m.final_balance}")


# ============================================================================
# SECTION 7: Reporter - CSV Export
# ============================================================================
print("\n" + "=" * 70)
print("SECTION 7: REPORTER - CSV EXPORT")
print("=" * 70)

print("""
WHAT: BacktestReporter exports results to CSV files.

WHY: After a backtest, you want to:
     - Analyze trades in a spreadsheet
     - Plot equity curve
     - Compare metrics across runs
     - Archive results

HOW: Takes a BacktestSession and writes CSV files.

EXPORT METHODS:
     - export_trades(path)
       Columns: trade_id, timestamp, symbol, side, direction, price, qty,
                notional, realized_pnl, commission, order_id, client_order_id

     - export_equity_curve(path)
       Columns: timestamp, equity, return_pct

     - export_metrics(path)
       Rows: metric name, value (one row per metric)

     - export_all(output_dir, prefix="")
       Exports all three files to a directory.
       Returns dict of file paths.

     - get_summary_dict()
       Returns metrics as Python dict (for programmatic access).

FILE NAMING:
     export_all() creates:
       {prefix}_{session_id}_trades.csv
       {prefix}_{session_id}_equity.csv
       {prefix}_{session_id}_metrics.csv

FILE: src/backtest/reporter.py

NOTE: Reporter auto-finalizes session if metrics are None.
      DB persistence is intentionally skipped - CSV export is the
      storage mechanism for backtest results.
""")

from backtest.reporter import BacktestReporter
import tempfile
import os

reporter = BacktestReporter(session)

print("\nReporter methods:")
print("  export_trades(path)      - Trade history CSV")
print("  export_equity_curve(path) - Equity over time CSV")
print("  export_metrics(path)     - Summary metrics CSV")
print("  export_all(output_dir)   - All exports to directory")
print("  get_summary_dict()       - Metrics as Python dict")

# Demo export
with tempfile.TemporaryDirectory() as tmpdir:
    paths = reporter.export_all(tmpdir, prefix="demo")

    print(f"\nExported files:")
    for name, path in paths.items():
        size = os.path.getsize(path)
        print(f"  {name}: {path.name} ({size} bytes)")

    # Show sample content
    print(f"\nSample from trades CSV:")
    with open(paths["trades"]) as f:
        for i, line in enumerate(f):
            if i < 3:
                print(f"  {line.rstrip()}")
            else:
                print("  ...")
                break

# Summary dict
print(f"\nget_summary_dict() sample:")
summary = reporter.get_summary_dict()
for key in ["total_trades", "net_pnl", "return_pct", "sharpe_ratio"]:
    print(f"  {key}: {summary[key]}")


print("\n" + "=" * 70)
print("WALKTHROUGH COMPLETE")
print("=" * 70)
print("\nKey takeaways:")
print("  1. Fill simulator uses trade-through model (price crosses limit)")
print("  2. Position tracker calculates realized/unrealized PnL")
print("  3. Order manager handles order lifecycle and fill detection")
print("  4. Session tracks equity curve, drawdown, and calculates metrics")
print("  5. Runner has two phases: process_fills() then execute_tick()")
print("  6. Engine orchestrates: funding → fills → equity → intents")
print("  7. Reporter exports results to CSV")
print()
