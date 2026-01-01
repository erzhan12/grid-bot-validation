# Grid Bot Backtest Implementation Plan

## Project Overview

This document provides a comprehensive step-by-step plan for implementing a grid bot backtesting system. The existing architecture provides a solid foundation with a strategy pattern, position management, limit order system, and data provider for historical ticker data.

## Current Architecture Analysis

### Core Components
- **Controller**: Main orchestrator that manages strategies and market makers (bms)
- **Strategy (Strat50)**: Grid trading strategy with greed-based order placement
- **BybitApiUsdt**: Market maker simulation with position management
- **Position**: Manages long/short positions with liquidation and margin calculations
- **LimitOrder & OrderManager**: Complete order lifecycle management
- **Greed**: Grid generation system with dynamic rebalancing
- **DataProvider**: Database iteration for historical ticker data

### Data Flow
1. Controller initializes strategies and market makers from config
2. Strategy iterates through historical ticker data via DataProvider
3. For each price tick, strategy processes greed orders and position management
4. Orders are placed/cancelled through Controller → BybitApiUsdt
5. Position calculations handle margin, liquidation ratios, and PnL

## Implementation Plan

### Phase 1: In-Memory Data Structures for Backtesting

#### 1.1 Historical Data (Existing)
Your existing `ticker_data` table and `DataProvider` class already handle historical price data iteration effectively. No changes needed here.

#### 1.2 In-Memory Data Structures for Backtest Session

**Create data classes for in-memory storage:**

```python
@dataclass
class BacktestTrade:
    trade_id: str
    symbol: str
    side: str  # 'Buy' or 'Sell'
    size: float
    price: float
    direction: str  # 'long' or 'short'
    executed_at: datetime
    order_id: str
    strategy_id: int
    bm_name: str
    realized_pnl: float = 0

@dataclass
class BacktestPositionSnapshot:
    timestamp: datetime
    symbol: str
    direction: str
    size: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    margin: float
    liquidation_price: float

@dataclass
class BacktestMetrics:
    symbol: str
    strategy_id: int
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0
    max_drawdown: float = 0
    max_profit: float = 0
    start_balance: float = 0
    end_balance: float = 0
    start_time: datetime = None
    end_time: datetime = None
    trades: List[BacktestTrade] = field(default_factory=list)
    position_history: List[BacktestPositionSnapshot] = field(default_factory=list)
```

#### 1.3 BacktestSession Class for Data Management

```python
class BacktestSession:
    """In-memory storage for single backtest run"""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.start_time = datetime.now()
        
        # In-memory storage
        self.trades: List[BacktestTrade] = []
        self.position_snapshots: List[BacktestPositionSnapshot] = []
        self.metrics: Dict[str, BacktestMetrics] = {}
        
        # Current state
        self.current_timestamp = None
        self.equity_curve: List[Tuple[datetime, float]] = []
        
    def record_trade(self, trade: BacktestTrade):
        """Record executed trade"""
        self.trades.append(trade)
        
    def record_position_snapshot(self, snapshot: BacktestPositionSnapshot):
        """Record position state at specific timestamp"""
        self.position_snapshots.append(snapshot)
        
    def update_equity(self, timestamp: datetime, total_equity: float):
        """Update equity curve"""
        self.equity_curve.append((timestamp, total_equity))
        
    def get_final_metrics(self) -> Dict[str, BacktestMetrics]:
        """Calculate and return final performance metrics"""
        for symbol in self.metrics:
            self._calculate_metrics(symbol)
        return self.metrics
```

### Phase 2: Order Management System Enhancement

#### 2.1 Extend LimitOrder System for Backtesting

**Key Features to Implement:**
- **In-Memory Order Storage**: Use existing `OrderManager` class with enhancements
- **Order Lifecycle Tracking**: Track creation, fills, cancellations in memory
- **Fill Logic Enhancement**: Improve price matching logic for realistic fills
- **Slippage Simulation**: Add configurable slippage for market realism

**Implementation Steps:**

1. **Enhance existing OrderManager class**:
```python
class BacktestOrderManager(OrderManager):
    def __init__(self, backtest_session: BacktestSession):
        super().__init__()
        self.backtest_session = backtest_session
        self.slippage_bps = 5  # 0.05% default slippage
        
    def create_order(self, symbol, side, limit_price, size, direction, 
                    strategy_id, bm_name, timestamp, callback=None):
        # Create order in memory using parent class
        order = super().create_order(symbol, side, limit_price, size, callback)
        
        # Add backtest-specific metadata
        order.direction = direction
        order.strategy_id = strategy_id
        order.bm_name = bm_name
        order.created_at = timestamp
        
        return order
        
    def check_fills(self, symbol, current_price, timestamp):
        # Check for fills with slippage simulation
        filled_orders = []
        
        for order_id, order in list(self.active_orders.items()):
            if order.symbol != symbol:
                continue
                
            if self._should_fill_with_slippage(order, current_price):
                fill_price = self._calculate_fill_price(order, current_price)
                if order.fill(fill_price, timestamp):
                    filled_orders.append(order)
                    
                    # Record trade in backtest session
                    trade = BacktestTrade(
                        trade_id=f"TRADE_{len(self.backtest_session.trades) + 1:06d}",
                        symbol=order.symbol,
                        side=order.side.value,
                        size=order.size,
                        price=fill_price,
                        direction=order.direction,
                        executed_at=timestamp,
                        order_id=order.order_id,
                        strategy_id=order.strategy_id,
                        bm_name=order.bm_name
                    )
                    self.backtest_session.record_trade(trade)
                    
                    # Remove from active orders
                    del self.active_orders[order_id]
                    
        return filled_orders
    
    def _should_fill_with_slippage(self, order, current_price):
        """Check if order should fill considering realistic market conditions"""
        if order.side == OrderSide.BUY:
            return current_price <= order.limit_price
        else:  # SELL
            return current_price >= order.limit_price
    
    def _calculate_fill_price(self, order, current_price):
        """Calculate realistic fill price with slippage"""
        slippage_factor = self.slippage_bps / 10000  # Convert basis points to decimal
        
        if order.side == OrderSide.BUY:
            # Buy orders may fill at slightly higher price due to slippage
            slippage = min(current_price * slippage_factor, 
                          abs(current_price - order.limit_price))
            return min(order.limit_price, current_price + slippage)
        else:  # SELL
            # Sell orders may fill at slightly lower price due to slippage
            slippage = min(current_price * slippage_factor,
                          abs(order.limit_price - current_price))
            return max(order.limit_price, current_price - slippage)
```

#### 2.2 Integration with BybitApiUsdt

**Modify BybitApiUsdt for Backtesting:**

1. **Add BacktestOrderManager to BybitApiUsdt**:
```python
class BybitApiUsdt:
    def __init__(self, APIKey, secret, amount, strat, name, controller):
        # ... existing initialization ...
        
        # Add backtest-specific components
        self.backtest_session = None  # Will be set by controller
        self.backtest_order_manager = None
        self.current_timestamp = None
        
    def init_backtest_mode(self, backtest_session):
        """Initialize backtesting mode"""
        self.backtest_session = backtest_session
        self.backtest_order_manager = BacktestOrderManager(backtest_session)
        
    def new_limit_order(self, side, symbol, price, bm_name, direction, amount=None):
        """Modified to work with backtest order manager"""
        l_price = BybitApiUsdt.round_price(symbol, price)
        
        if amount is None:
            l_amount = self.__get_amount(symbol, l_price, side=side, bm_name=bm_name)
        else:
            l_amount = amount
            
        l_amount_multiplier = self.__get_amount_multiplier(symbol, side, l_price, direction)
        l_amount = self.round_amount(l_amount * l_amount_multiplier)
        
        if self._is_good_to_place(symbol, l_price, l_amount, side, direction, False):
            # Create backtest order instead of API call
            if self.backtest_order_manager:
                order = self.backtest_order_manager.create_order(
                    symbol=symbol,
                    side=OrderSide.BUY if side == 'Buy' else OrderSide.SELL,
                    limit_price=l_price,
                    size=l_amount,
                    direction=direction,
                    strategy_id=self.strat.id,
                    bm_name=bm_name,
                    timestamp=self.current_timestamp
                )
                return l_price, order.order_id
            else:
                # Fallback to original API logic for live trading
                return self._place_active_order(symbol, side, l_amount, l_price, False, 0)
        
        return 0, ""
    
    def check_and_fill_orders(self, symbol, current_price, timestamp):
        """Check for order fills and update positions"""
        if self.backtest_order_manager:
            self.current_timestamp = timestamp
            filled_orders = self.backtest_order_manager.check_fills(
                symbol, current_price, timestamp
            )
            
            for order in filled_orders:
                self._process_filled_order(order, current_price, timestamp)
                
    def _process_filled_order(self, order, current_price, timestamp):
        """Process a filled order and update positions"""
        direction = Direction.LONG if order.direction == 'long' else Direction.SHORT
        position = self.position[direction]
        
        # Update position based on fill
        if order.side == OrderSide.BUY:
            if direction == Direction.LONG:
                position.increase_position(order.size, order.fill_price)
            else:
                position.reduce_position(order.size, order.fill_price)
        else:  # SELL
            if direction == Direction.SHORT:
                position.increase_position(order.size, order.fill_price)
            else:
                position.reduce_position(order.size, order.fill_price)
        
        # Update position with current market data
        position.update_current_price(current_price, timestamp)
```

### Phase 3: Position Management and PnL Calculation

#### 3.1 Enhance Position Class for Backtesting

**Bybit Position Logic Implementation:**

1. **Position Size Calculation**:
```python
def update_position_from_fill(self, fill_side, fill_size, fill_price, timestamp):
    """Update position based on order fill following Bybit logic"""
    if fill_side == 'Buy':
        if self.direction == Direction.LONG:
            # Increase long position
            self._increase_position(fill_size, fill_price)
        else:
            # Reduce short position
            self._reduce_position(fill_size, fill_price)
    else:  # Sell
        if self.direction == Direction.SHORT:
            # Increase short position
            self._increase_position(fill_size, fill_price)
        else:
            # Reduce long position
            self._reduce_position(fill_size, fill_price)
```

2. **PnL Calculation (Bybit Logic)**:
```python
def calculate_unrealized_pnl(self, current_price):
    """Calculate unrealized PnL following Bybit's methodology"""
    if self.size == 0:
        return 0
    
    if self.direction == Direction.LONG:
        # Long: PnL = (Current Price - Entry Price) * Size
        return (current_price - self.entry_price) * self.size
    else:
        # Short: PnL = (Entry Price - Current Price) * Size
        return (self.entry_price - current_price) * self.size

def calculate_realized_pnl(self, exit_price, exit_size):
    """Calculate realized PnL for partial/full position close"""
    if self.direction == Direction.LONG:
        return (exit_price - self.entry_price) * exit_size
    else:
        return (self.entry_price - exit_price) * exit_size
```

3. **Liquidation Price Calculation**:
```python
def calculate_liquidation_price(self, wallet_balance, maintenance_margin_rate=0.01):
    """Calculate liquidation price following Bybit's formula"""
    if self.size == 0:
        return 0
    
    # Bybit liquidation formula for USDT perpetual
    # Liq Price = (Entry Price ± Wallet Balance) / (Size ± Size * MMR)
    if self.direction == Direction.LONG:
        numerator = self.entry_price * self.size - wallet_balance
        denominator = self.size * (1 - maintenance_margin_rate)
    else:
        numerator = self.entry_price * self.size + wallet_balance
        denominator = self.size * (1 + maintenance_margin_rate)
    
    return numerator / denominator if denominator != 0 else 0
```

#### 3.2 Margin and Leverage Management

```python
class MarginManager:
    def __init__(self, initial_balance, max_leverage=100):
        self.initial_balance = initial_balance
        self.available_balance = initial_balance
        self.used_margin = 0
        self.max_leverage = max_leverage
    
    def calculate_required_margin(self, position_value, leverage):
        """Calculate margin required for position"""
        return position_value / leverage
    
    def can_open_position(self, position_value, leverage):
        """Check if sufficient margin available"""
        required_margin = self.calculate_required_margin(position_value, leverage)
        return self.available_balance >= required_margin
    
    def allocate_margin(self, position_value, leverage):
        """Allocate margin for new position"""
        required_margin = self.calculate_required_margin(position_value, leverage)
        if self.can_open_position(position_value, leverage):
            self.available_balance -= required_margin
            self.used_margin += required_margin
            return True
        return False
```

### Phase 4: Backtesting Engine Core

#### 4.1 Create BacktestEngine Class

```python
class BacktestEngine:
    def __init__(self, config, start_date=None, end_date=None):
        self.config = config
        self.start_date = start_date
        self.end_date = end_date
        self.session_id = self._generate_session_id()
        self.backtest_session = BacktestSession(self.session_id)
        self.controllers = {}
        
    def run_backtest(self, symbol):
        """Main backtesting loop for a single symbol"""
        print(f"Starting backtest for {symbol}")
        
        # Initialize controller for symbol
        controller = Controller(symbol)
        
        # Initialize backtest mode for all market makers
        for bm in controller.bms:
            bm.init_backtest_mode(self.backtest_session)
            
        # Run the backtest by leveraging existing check_job logic
        # The existing Strat50._check_pair_step will iterate through historical data
        controller.check_job()
        
        # Generate final metrics
        final_metrics = self.backtest_session.get_final_metrics()
        
        print(f"Backtest completed for {symbol}")
        print(f"Total trades: {len(self.backtest_session.trades)}")
        
        return final_metrics
        
    def _generate_session_id(self):
        """Generate unique session ID"""
        from datetime import datetime
        return f"BT_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
```

#### 4.2 Metrics Calculation in BacktestSession

```python
# Add this method to BacktestSession class
def _calculate_metrics(self, symbol):
    """Calculate performance metrics for a symbol"""
    symbol_trades = [t for t in self.trades if t.symbol == symbol]
    
    if not symbol_trades:
        return
        
    winning_trades = [t for t in symbol_trades if t.realized_pnl > 0]
    losing_trades = [t for t in symbol_trades if t.realized_pnl < 0]
    
    total_pnl = sum(t.realized_pnl for t in symbol_trades)
    total_winning_pnl = sum(t.realized_pnl for t in winning_trades)
    total_losing_pnl = sum(t.realized_pnl for t in losing_trades)
    
    metrics = BacktestMetrics(
        symbol=symbol,
        strategy_id=symbol_trades[0].strategy_id if symbol_trades else 0,
        total_trades=len(symbol_trades),
        winning_trades=len(winning_trades),
        total_pnl=total_pnl,
        start_time=symbol_trades[0].executed_at if symbol_trades else None,
        end_time=symbol_trades[-1].executed_at if symbol_trades else None,
        trades=symbol_trades,
        position_history=self.position_snapshots
    )
    
    # Calculate additional metrics
    if len(symbol_trades) > 0:
        metrics.win_rate = len(winning_trades) / len(symbol_trades)
        if total_losing_pnl != 0:
            metrics.profit_factor = abs(total_winning_pnl / total_losing_pnl)
    
    self.metrics[symbol] = metrics
```

### Phase 5: Enhanced Strategy Integration

#### 5.1 Modify Strat50 for Full Backtesting

```python
def _process_single_last_close(self, last_close, timestamp):
    """Enhanced processing with full order and position management"""
    self.last_close = last_close
    
    # Set current timestamp for all market makers
    for bm in self.bms:
        if self.id == bm.strat:
            bm.current_timestamp = timestamp
    
    # Check for order fills first (CRITICAL: before position updates)
    self._check_order_fills(last_close, timestamp)
    
    # Update positions with current price
    self._update_positions(last_close, timestamp)
    
    # Build/update greed grid
    while len(self.greed.greed) <= 1:
        self.greed.build_greed(last_close)
    
    # Check positions ratio and margin
    self.check_positions_ratio(timestamp)
    
    # Place new orders based on grid
    self._check_and_place('long')
    self._check_and_place('short')
    
    # Record position snapshots for metrics
    self._record_position_snapshots(timestamp)

def _check_order_fills(self, current_price, timestamp):
    """Check and process order fills"""
    for bm in self.bms:
        if self.id == bm.strat:
            bm.check_and_fill_orders(self._symbol, current_price, timestamp)

def _update_positions(self, current_price, timestamp):
    """Update position values and PnL"""
    for bm in self.bms:
        if self.id == bm.strat:
            # Update current price for both positions
            long_pos = bm.position[Direction.LONG]
            short_pos = bm.position[Direction.SHORT]
            
            # Update unrealized PnL
            if not long_pos.is_empty():
                long_pos.update_unrealized_pnl(current_price)
            if not short_pos.is_empty():
                short_pos.update_unrealized_pnl(current_price)

def _record_position_snapshots(self, timestamp):
    """Record current position state for analysis"""
    for bm in self.bms:
        if self.id == bm.strat and bm.backtest_session:
            # Record long position snapshot
            long_pos = bm.position[Direction.LONG]
            if not long_pos.is_empty():
                snapshot = BacktestPositionSnapshot(
                    timestamp=timestamp,
                    symbol=self._symbol,
                    direction='long',
                    size=long_pos.size,
                    entry_price=long_pos.entry_price,
                    current_price=self.last_close,
                    unrealized_pnl=long_pos.calculate_unrealized_pnl(self.last_close),
                    margin=long_pos.get_margin(),
                    liquidation_price=long_pos.liq_price
                )
                bm.backtest_session.record_position_snapshot(snapshot)
            
            # Record short position snapshot
            short_pos = bm.position[Direction.SHORT]
            if not short_pos.is_empty():
                snapshot = BacktestPositionSnapshot(
                    timestamp=timestamp,
                    symbol=self._symbol,
                    direction='short',
                    size=short_pos.size,
                    entry_price=short_pos.entry_price,
                    current_price=self.last_close,
                    unrealized_pnl=short_pos.calculate_unrealized_pnl(self.last_close),
                    margin=short_pos.get_margin(),
                    liquidation_price=short_pos.liq_price
                )
                bm.backtest_session.record_position_snapshot(snapshot)
```

### Phase 6: Configuration and Testing

#### 6.1 Backtest Configuration

**Add to config.yaml**:
```yaml
backtest:
  initial_balance: 10000
  max_leverage: 100
  slippage_bps: 5  # 0.05%
  commission_rate: 0.0006  # 0.06%
  
risk_management:
  max_position_size_pct: 20  # 20% of balance per position
  max_daily_loss_pct: 5     # 5% daily loss limit
  liquidation_buffer: 0.02  # 2% buffer from liquidation
```

#### 6.2 Create Simple Backtest Runner

```python
# backtest_runner.py
from src.backtest_engine import BacktestEngine
from config.settings import settings

def run_backtest(symbol):
    """Run backtest for specified symbol using existing data in database"""
    
    print(f"Starting backtest for {symbol}")
    
    # Initialize and run backtest (uses existing DataProvider to read from DB)
    engine = BacktestEngine(settings)
    results = engine.run_backtest(symbol)
    
    # Print summary
    if symbol in results:
        metrics = results[symbol]
        print(f"\n=== BACKTEST RESULTS for {symbol} ===")
        print(f"Total Trades: {metrics.total_trades}")
        print(f"Winning Trades: {metrics.winning_trades}")
        print(f"Win Rate: {metrics.winning_trades/metrics.total_trades*100:.2f}%" if metrics.total_trades > 0 else "No trades")
        print(f"Total PnL: {metrics.total_pnl:.2f}")
        print(f"Start Time: {metrics.start_time}")
        print(f"End Time: {metrics.end_time}")
    
    return results

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python backtest_runner.py <symbol>")
        print("Example: python backtest_runner.py BTCUSDT")
        sys.exit(1)
    
    symbol = sys.argv[1]
    results = run_backtest(symbol)
```

### Phase 7: Reporting and Analysis

#### 7.1 In-Memory Report Generation

```python
class BacktestReporter:
    def __init__(self, backtest_session: BacktestSession):
        self.session = backtest_session
        
    def generate_summary_report(self):
        """Generate comprehensive backtest summary from in-memory data"""
        return {
            'session_info': {
                'session_id': self.session.session_id,
                'start_time': self.session.start_time,
                'total_trades': len(self.session.trades)
            },
            'performance_metrics': self._calculate_performance_metrics(),
            'trade_analysis': self._analyze_trades(),
            'position_analysis': self._analyze_positions()
        }
    
    def _calculate_performance_metrics(self):
        """Calculate key performance metrics"""
        trades = self.session.trades
        if not trades:
            return {}
            
        total_pnl = sum(t.realized_pnl for t in trades)
        winning_trades = [t for t in trades if t.realized_pnl > 0]
        losing_trades = [t for t in trades if t.realized_pnl < 0]
        
        return {
            'total_trades': len(trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': len(winning_trades) / len(trades) if trades else 0,
            'total_pnl': total_pnl,
            'avg_win': sum(t.realized_pnl for t in winning_trades) / len(winning_trades) if winning_trades else 0,
            'avg_loss': sum(t.realized_pnl for t in losing_trades) / len(losing_trades) if losing_trades else 0,
            'largest_win': max((t.realized_pnl for t in winning_trades), default=0),
            'largest_loss': min((t.realized_pnl for t in losing_trades), default=0)
        }
    
    def export_to_csv(self, output_dir="./backtest_results"):
        """Export detailed results to CSV files"""
        import csv
        import os
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Export trades
        trades_file = os.path.join(output_dir, f"trades_{self.session.session_id}.csv")
        with open(trades_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Trade ID', 'Symbol', 'Side', 'Size', 'Price', 'Direction', 'Executed At', 'Realized PnL'])
            for trade in self.session.trades:
                writer.writerow([trade.trade_id, trade.symbol, trade.side, trade.size, 
                               trade.price, trade.direction, trade.executed_at, trade.realized_pnl])
        
        # Export position snapshots
        positions_file = os.path.join(output_dir, f"positions_{self.session.session_id}.csv")
        with open(positions_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Timestamp', 'Symbol', 'Direction', 'Size', 'Entry Price', 
                           'Current Price', 'Unrealized PnL', 'Margin'])
            for pos in self.session.position_snapshots:
                writer.writerow([pos.timestamp, pos.symbol, pos.direction, pos.size,
                               pos.entry_price, pos.current_price, pos.unrealized_pnl, pos.margin])
        
        print(f"Results exported to {output_dir}")
```

### Phase 8: Order Processing Logic Details

#### 8.1 Order Creation Process

1. **Greed Grid Analysis**: Strategy calculates grid levels based on current price
2. **Order Validation**: Check margin requirements and risk limits
3. **Order Placement**: Create limit order in system
4. **Database Storage**: Persist order details
5. **Order Tracking**: Add to active orders list

#### 8.2 Order Fill Processing

1. **Price Matching**: Check if current price triggers order
   - Buy orders: fill when market price ≤ limit price
   - Sell orders: fill when market price ≥ limit price
2. **Slippage Application**: Apply realistic slippage based on market conditions
3. **Position Update**: Modify position size and entry price
4. **PnL Calculation**: Update realized/unrealized PnL
5. **Margin Adjustment**: Update margin usage
6. **Database Update**: Record trade execution

#### 8.3 Order Cancellation Logic

1. **Grid Rebalancing**: Cancel orders outside current grid range
2. **Risk Management**: Cancel orders that exceed risk limits
3. **Strategy Changes**: Cancel orders when strategy parameters change
4. **Database Cleanup**: Update order status to cancelled

### Phase 9: Position and PnL Calculation Details

#### 9.1 Position Size Management

**Opening Position**:
```python
def open_position(self, side, size, price):
    if self.size == 0:
        # New position
        self.size = size
        self.entry_price = price
        self.side = side
    else:
        # Add to existing position (same side)
        total_value = (self.size * self.entry_price) + (size * price)
        self.size += size
        self.entry_price = total_value / self.size  # Average entry price
```

**Closing Position**:
```python
def close_position(self, close_size, close_price):
    if close_size >= self.size:
        # Full close
        realized_pnl = self.calculate_realized_pnl(close_price, self.size)
        self.size = 0
        self.entry_price = 0
    else:
        # Partial close
        realized_pnl = self.calculate_realized_pnl(close_price, close_size)
        self.size -= close_size
        # Entry price remains the same for remaining position
```

#### 9.2 PnL Calculation Specifics

**Bybit USDT Perpetual PnL Formula**:
- **Long Position**: `Unrealized PnL = (Mark Price - Entry Price) × Size`
- **Short Position**: `Unrealized PnL = (Entry Price - Mark Price) × Size`
- **Realized PnL**: Calculated when position is closed or reduced

**ROE (Return on Equity)**:
```python
def calculate_roe(self):
    """Calculate ROE percentage"""
    if self.initial_margin == 0:
        return 0
    return (self.unrealized_pnl / self.initial_margin) * 100
```

### Phase 8: Implementation Priorities

#### Priority 1 (Core Functionality)
1. Create in-memory data structures (BacktestSession, BacktestTrade, etc.)
2. Implement BacktestOrderManager with in-memory storage
3. Enhance Position class with PnL calculation methods
4. Create basic BacktestEngine class

#### Priority 2 (Strategy Integration)
5. Modify BybitApiUsdt to support backtest mode
6. Enhance Strat50 with order fill checking and position snapshots
7. Integrate order management with existing greed system

#### Priority 3 (Reporting and Analysis)
8. Implement metrics calculation in BacktestSession
9. Create BacktestReporter for summary and CSV export
10. Add performance tracking and analysis

#### Priority 4 (Testing and Optimization)
11. Test with existing historical data
12. Validate results against manual calculations
13. Performance optimization for large datasets

### Phase 9: Testing Strategy

1. **Unit Tests**: Test individual components (Position, OrderManager, BacktestSession)
2. **Integration Tests**: Test component interactions with real data flow
3. **Backtest Validation**: Compare results with manual calculations
4. **Performance Tests**: Ensure system handles large datasets efficiently

### Phase 10: Key Implementation Notes

1. **Maintain Existing Architecture**: Build upon current structure without breaking changes
2. **In-Memory Storage**: All backtest data lives in memory during execution
3. **Realistic Simulation**: Implement proper slippage and fill logic
4. **Leverage Existing Data**: Use your existing ticker_data table and DataProvider
5. **Modular Design**: Keep components loosely coupled for testability
6. **Performance Focus**: Optimize for processing large historical datasets in memory

## Quick Start Implementation

To get started immediately, implement in this order:

1. **Create BacktestSession and data classes** (30 minutes)
2. **Create BacktestOrderManager** extending existing OrderManager (1 hour)
3. **Add backtest mode to BybitApiUsdt** (1 hour)
4. **Enhance Strat50 with order fill checking** (30 minutes)
5. **Create simple BacktestEngine** (30 minutes)
6. **Test with a small data sample** (30 minutes)

Total: **4 hours for basic working backtest system**

This plan provides a comprehensive roadmap for implementing a grid bot backtesting system using in-memory storage that leverages your existing architecture and historical data infrastructure.
