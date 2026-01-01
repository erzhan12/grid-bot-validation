# Position Management Implementation Plan

## Overview

This plan details the implementation of position management methods in the grid bot backtest system, focusing on realistic position tracking, average price calculation, and PnL management following Bybit's perpetual futures logic.

## Current State Analysis

### Existing Placeholder Methods (Need Implementation)
```python
# In src/bybit_api_usdt.py
def _increase_position(self, position, size, price):     # Line 94-97
def _reduce_position(self, position, size, price):       # Line 99-102  
def _update_position_current_price(self, position, current_price, timestamp):  # Line 104-107
```

### Current Position Class Structure
```python
# In src/position.py
class Position:
    - size: float (current position size)
    - entry_price: float (average entry price)
    - direction: Direction (LONG/SHORT)
    - margin calculations
    - liquidation price calculations
```

## Implementation Plan

### Phase 1: Enhanced Position Data Structure

#### 1.1 Create PositionTracker Class
```python
# New file: src/position_tracker.py
@dataclass
class PositionEntry:
    """Individual position entry for tracking multiple fills"""
    size: float
    price: float
    timestamp: datetime
    order_id: str
    
@dataclass
class PositionState:
    """Complete position state tracking"""
    total_size: float = 0.0
    average_entry_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0
    entries: List[PositionEntry] = field(default_factory=list)
    
class PositionTracker:
    """Advanced position tracking with average price calculation"""
    
    def __init__(self, direction: Direction, initial_balance: float):
        self.direction = direction
        self.state = PositionState()
        self.balance = initial_balance
        self.commission_rate = 0.0006  # 0.06%
        
    def add_position(self, size: float, price: float, timestamp: datetime, order_id: str) -> float:
        """Add to position and return realized PnL"""
        
    def reduce_position(self, size: float, price: float, timestamp: datetime, order_id: str) -> float:
        """Reduce position and return realized PnL"""
        
    def calculate_average_price(self) -> float:
        """Calculate volume-weighted average price"""
        
    def calculate_unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized PnL based on current market price"""
```

#### 1.2 Position Entry Logic (FIFO vs Average Price)

**Two Approaches for Position Management:**

1. **FIFO (First In, First Out)** - More complex but accurate for tax purposes
2. **Average Price** - Simpler, commonly used by exchanges

**Recommended: Average Price Method (Bybit Standard)**

```python
def add_position(self, size: float, price: float, timestamp: datetime, order_id: str) -> float:
    """
    Add to position using average price calculation
    
    Formula: New Avg Price = (Current Total Value + New Value) / (Current Size + New Size)
    """
    current_total_value = self.state.total_size * self.state.average_entry_price
    new_value = size * price
    new_total_size = self.state.total_size + size
    
    # Calculate new average entry price
    if new_total_size > 0:
        self.state.average_entry_price = (current_total_value + new_value) / new_total_size
    
    self.state.total_size = new_total_size
    
    # Record the entry
    entry = PositionEntry(size, price, timestamp, order_id)
    self.state.entries.append(entry)
    
    # Calculate commission
    commission = size * price * self.commission_rate
    
    return -commission  # Negative because it's a cost
```

### Phase 2: Position Increase/Decrease Logic

#### 2.1 Position Increase Implementation

```python
def _increase_position(self, position, size, price):
    """
    Increase position size with proper average price calculation
    
    Args:
        position: Position object to modify
        size: Size to add to position
        price: Price of the new position entry
    """
    
    # Get position tracker (create if doesn't exist)
    if not hasattr(position, 'tracker'):
        position.tracker = PositionTracker(position.direction, self.initial_balance)
    
    # Add to position
    realized_pnl = position.tracker.add_position(
        size=size,
        price=price,
        timestamp=self.current_timestamp,
        order_id=f"FILL_{len(position.tracker.state.entries) + 1}"
    )
    
    # Update position object
    position.size = position.tracker.state.total_size
    position.entry_price = position.tracker.state.average_entry_price
    
    # Record realized PnL (commission in this case)
    if self.backtest_session:
        # Find the corresponding trade and update its realized PnL
        for trade in reversed(self.backtest_session.trades):
            if trade.size == size and abs(trade.price - price) < 0.01:
                trade.realized_pnl = realized_pnl
                break
    
    return realized_pnl
```

#### 2.2 Position Decrease Implementation

```python
def _reduce_position(self, position, size, price):
    """
    Reduce position size with proper PnL realization
    
    Args:
        position: Position object to modify
        size: Size to reduce from position
        price: Price of the position exit
    """
    
    if not hasattr(position, 'tracker'):
        # No position to reduce
        return 0.0
    
    # Calculate realized PnL before reducing
    realized_pnl = position.tracker.reduce_position(
        size=size,
        price=price,
        timestamp=self.current_timestamp,
        order_id=f"CLOSE_{len(position.tracker.state.entries) + 1}"
    )
    
    # Update position object
    position.size = position.tracker.state.total_size
    if position.size > 0:
        position.entry_price = position.tracker.state.average_entry_price
    else:
        position.entry_price = 0.0
    
    # Record realized PnL
    if self.backtest_session:
        for trade in reversed(self.backtest_session.trades):
            if trade.size == size and abs(trade.price - price) < 0.01:
                trade.realized_pnl = realized_pnl
                break
    
    return realized_pnl
```

### Phase 3: Current Price Update and Unrealized PnL

#### 3.1 Current Price Update Implementation

```python
def _update_position_current_price(self, position, current_price, timestamp):
    """
    Update position with current market price and calculate unrealized PnL
    
    Args:
        position: Position object to update
        current_price: Current market price
        timestamp: Current timestamp
    """
    
    if not hasattr(position, 'tracker') or position.size == 0:
        return
    
    # Calculate unrealized PnL
    unrealized_pnl = position.tracker.calculate_unrealized_pnl(current_price)
    
    # Update position state
    position.tracker.state.unrealized_pnl = unrealized_pnl
    
    # Update margin calculations (simplified)
    position_value = position.size * current_price
    position.tracker.state.margin_used = position_value / 10  # 10x leverage assumption
    
    # Calculate liquidation price
    liquidation_price = self._calculate_liquidation_price(position, current_price)
    
    # Update position object attributes
    if hasattr(position, 'update_unrealized_pnl'):
        position.update_unrealized_pnl(unrealized_pnl)
    
    # Record position snapshot for backtesting
    if self.backtest_session:
        self._record_position_snapshot(position, current_price, timestamp)
```

### Phase 4: Bybit-Specific PnL Calculations

#### 4.1 Unrealized PnL Calculation (Bybit Logic)

```python
def calculate_unrealized_pnl(self, current_price: float) -> float:
    """
    Calculate unrealized PnL using Bybit's methodology
    
    For USDT Perpetual:
    Long: PnL = (Mark Price - Avg Entry Price) × Position Size
    Short: PnL = (Avg Entry Price - Mark Price) × Position Size
    """
    
    if self.state.total_size == 0:
        return 0.0
    
    if self.direction == Direction.LONG:
        # Long position: profit when price goes up
        return (current_price - self.state.average_entry_price) * self.state.total_size
    else:
        # Short position: profit when price goes down  
        return (self.state.average_entry_price - current_price) * self.state.total_size
```

#### 4.2 Realized PnL Calculation

```python
def reduce_position(self, size: float, price: float, timestamp: datetime, order_id: str) -> float:
    """
    Reduce position and calculate realized PnL
    """
    
    if self.state.total_size == 0 or size > self.state.total_size:
        return 0.0  # Cannot reduce non-existent or over-reduce position
    
    # Calculate realized PnL on the closed portion
    if self.direction == Direction.LONG:
        # Long position: PnL = (Exit Price - Avg Entry Price) × Closed Size
        pnl_per_unit = price - self.state.average_entry_price
    else:
        # Short position: PnL = (Avg Entry Price - Exit Price) × Closed Size  
        pnl_per_unit = self.state.average_entry_price - price
    
    realized_pnl = pnl_per_unit * size
    
    # Subtract commission
    commission = size * price * self.commission_rate
    realized_pnl -= commission
    
    # Update position state
    self.state.total_size -= size
    self.state.realized_pnl += realized_pnl
    
    # Average entry price stays the same for remaining position
    # (This is Bybit's approach)
    
    # Record the reduction
    entry = PositionEntry(-size, price, timestamp, order_id)  # Negative size for reduction
    self.state.entries.append(entry)
    
    return realized_pnl
```

### Phase 5: Liquidation Price Calculation

#### 5.1 Bybit Liquidation Formula Implementation

```python
def _calculate_liquidation_price(self, position, current_price):
    """
    Calculate liquidation price using Bybit's formula for USDT Perpetual
    
    Formula:
    Long: Liq Price = (WB + TMM1 - TMM2 - (Side1 * Size1 * MarkPrice1)) / 
                      (Size1 * (Side1 - MMR))
    Short: Similar but with opposite sides
    
    Simplified for single position:
    Long: Liq Price = (Entry Price * Size - Available Balance) / 
                      (Size * (1 - MMR))
    """
    
    if not hasattr(position, 'tracker') or position.size == 0:
        return 0.0
    
    maintenance_margin_rate = 0.01  # 1% MMR for most pairs
    
    # Available balance (simplified - in real scenario this is complex)
    available_balance = self.backtest_session.current_balance * 0.8  # Conservative estimate
    
    if position.direction == Direction.LONG:
        # Long liquidation price
        numerator = (position.entry_price * position.size) - available_balance
        denominator = position.size * (1 - maintenance_margin_rate)
    else:
        # Short liquidation price  
        numerator = (position.entry_price * position.size) + available_balance
        denominator = position.size * (1 + maintenance_margin_rate)
    
    if denominator == 0:
        return 0.0
    
    liq_price = numerator / denominator
    
    # Ensure liquidation price is reasonable
    if position.direction == Direction.LONG:
        liq_price = max(liq_price, position.entry_price * 0.5)  # Not below 50% of entry
    else:
        liq_price = min(liq_price, position.entry_price * 1.5)  # Not above 150% of entry
    
    return liq_price
```

### Phase 6: Integration with Existing System

#### 6.1 Enhance Position Class

```python
# Modify src/position.py
class Position:
    def __init__(self, direction, strat):
        # ... existing code ...
        self.tracker = None  # Will be initialized when needed
        
    def update_unrealized_pnl(self, unrealized_pnl):
        """Update unrealized PnL"""
        self._unrealized_pnl = unrealized_pnl
        
    def get_unrealized_pnl(self):
        """Get current unrealized PnL"""
        return getattr(self, '_unrealized_pnl', 0.0)
        
    def get_realized_pnl(self):
        """Get total realized PnL"""
        if self.tracker:
            return self.tracker.state.realized_pnl
        return 0.0
        
    def get_total_pnl(self):
        """Get total PnL (realized + unrealized)"""
        return self.get_realized_pnl() + self.get_unrealized_pnl()
```

#### 6.2 Update Order Fill Processing

```python
# In src/bybit_api_usdt.py - enhance _process_filled_order
def _process_filled_order(self, order, current_price, timestamp):
    """Process a filled order and update positions"""
    from src.limit_order import OrderSide
    
    direction = Direction.LONG if order.direction == 'long' else Direction.SHORT
    position = self.position[direction]
    
    # Determine if this increases or decreases the position
    if order.side == OrderSide.BUY:
        if direction == Direction.LONG:
            # Buying for long position = increase
            self._increase_position(position, order.size, order.fill_price)
        else:
            # Buying to cover short position = decrease
            self._reduce_position(position, order.size, order.fill_price)
    else:  # SELL
        if direction == Direction.SHORT:
            # Selling for short position = increase
            self._increase_position(position, order.size, order.fill_price)
        else:
            # Selling long position = decrease
            self._reduce_position(position, order.size, order.fill_price)
    
    # Update with current market price
    self._update_position_current_price(position, current_price, timestamp)
```

### Phase 7: Testing and Validation

#### 7.1 Position Management Tests

```python
# New file: tests/test_position_management.py
def test_position_increase_average_price():
    """Test position increase with average price calculation"""
    # Buy 0.1 BTC at $50,000
    # Buy 0.1 BTC at $51,000  
    # Average should be $50,500
    
def test_position_decrease_realized_pnl():
    """Test position decrease with PnL realization"""
    # Open position at $50,000
    # Close 50% at $51,000
    # Should realize profit of $100 (minus commission)
    
def test_liquidation_price_calculation():
    """Test Bybit liquidation price formula"""
    # Test various position sizes and entry prices
    
def test_commission_calculation():
    """Test commission impact on PnL"""
    # Verify commissions are properly deducted
```

#### 7.2 Integration Tests

```python
def test_full_grid_trading_cycle():
    """Test complete grid trading cycle with position management"""
    # 1. Start with empty positions
    # 2. Fill buy order (increase long position)
    # 3. Fill sell order (increase short position)  
    # 4. Price moves, fill opposite orders (decrease positions)
    # 5. Verify PnL calculations at each step
```

### Phase 8: Performance Optimization

#### 8.1 Memory Efficiency
- Limit position entry history to last N entries
- Aggregate old entries for long-running backtests
- Optimize PnL calculations for high-frequency updates

#### 8.2 Calculation Efficiency
- Cache liquidation price calculations
- Use incremental PnL updates instead of full recalculation
- Batch position updates for multiple fills

## Implementation Timeline

### Week 1: Core Position Tracking
- [ ] Create PositionTracker class
- [ ] Implement average price calculation
- [ ] Basic increase/decrease position methods

### Week 2: PnL Calculations  
- [ ] Implement unrealized PnL calculation
- [ ] Implement realized PnL calculation
- [ ] Add commission handling

### Week 3: Advanced Features
- [ ] Liquidation price calculation
- [ ] Position snapshot recording
- [ ] Integration with existing Position class

### Week 4: Testing and Optimization
- [ ] Comprehensive test suite
- [ ] Performance optimization
- [ ] Documentation and examples

## Success Metrics

- [ ] **Accuracy**: PnL calculations match manual verification
- [ ] **Performance**: Can process 10,000+ price ticks without lag
- [ ] **Integration**: Works seamlessly with existing backtest system
- [ ] **Validation**: Liquidation prices are realistic and accurate

## Expected Benefits

1. **Realistic Position Tracking**: Proper average pricing and PnL calculation
2. **Accurate Backtests**: Results that match real trading scenarios  
3. **Risk Management**: Proper liquidation price calculations
4. **Performance Analysis**: Detailed realized vs unrealized PnL breakdown
5. **Strategy Optimization**: Better insights into grid trading profitability

This implementation will transform the placeholder position methods into a sophisticated position management system that accurately simulates real Bybit trading behavior.
