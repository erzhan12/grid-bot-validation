# Position Field Calculation Testing

This document describes the position field calculation test scripts created to validate margin, liquidation price, unrealized PnL, and other position metrics.

## Test Scripts

### 1. Comprehensive Test Suite: `tests/test_position_field_calculations.py`

A comprehensive test script that validates all position calculations with multiple scenarios:

**Features:**
- ✅ Basic long/short position calculations
- ✅ Multiple leverage scenarios (2x-50x)
- ✅ Edge cases with extreme values
- ✅ Multi-entry average price calculations
- ✅ Cross-validation between legacy and new position systems
- ✅ User scenario validation

**Run the complete test:**
```bash
# Run all tests with detailed output
PYTHONPATH=. python tests/test_position_field_calculations.py

# Run as pytest (test functions only)
pytest tests/test_position_field_calculations.py -v
```

### 2. Interactive Position Calculator: `test_my_position.py`

A user-friendly script for calculating position metrics with your own parameters.

**Run the calculator:**
```bash
python test_my_position.py
```

**Example usage:**
```
🎯 Position Calculator
========================================
Enter position size (e.g., 0.1): 0.1
Enter entry price (e.g., 50000): 50000
Enter current price (e.g., 55000): 55000
Enter total balance (e.g., 10000): 10000
Enter direction (long/short) [default: long]: long
Enter leverage (e.g., 10) [default: 10]: 10
Enter symbol (e.g., BTCUSDT) [default: BTCUSDT]: BTCUSDT
```

## Position Metrics Calculated

### Core Metrics
- **Position Value**: `size * current_price`
- **Margin Used**: `position_value / leverage`
- **Unrealized PnL**:
  - Long: `(current_price - entry_price) * size`
  - Short: `(entry_price - current_price) * size`

### Risk Metrics
- **Liquidation Price**: Using simplified Bybit formula
  - Long: `entry_price - (entry_price / leverage) * (1 - maintenance_margin_rate)`
  - Short: `entry_price + (entry_price / leverage) * (1 + maintenance_margin_rate)`
- **Liquidation Ratio**: `liquidation_price / current_price`
- **ROE (Return on Equity)**: `(unrealized_pnl / margin_used) * 100`

### Portfolio Metrics
- **Margin Percentage**: `(margin_used / total_balance) * 100`
- **Distance to Liquidation**: `abs(1 - liquidation_ratio) * 100`

## Key Parameters

### Required Inputs
- **Position Size**: Amount of the asset (e.g., 0.1 BTC)
- **Entry Price**: Price at which position was opened
- **Current Price**: Current market price
- **Total Balance**: Total account balance

### Additional Parameters
- **Direction**: LONG or SHORT (default: LONG)
- **Leverage**: Leverage multiplier (default: 10x)
- **Symbol**: Trading pair (default: BTCUSDT)
- **Commission Rate**: Trading commission (default: 0.02%)
- **Maintenance Margin Rate**: MMR for liquidations (default: 1.0%)

## Test Scenarios Included

### 1. Basic Scenarios
- Long position with profit
- Short position with profit
- Different leverage levels (2x, 5x, 10x, 25x, 50x)

### 2. Edge Cases
- Very small positions (0.001)
- Very large positions (10.0)
- Extreme profits and losses
- Near-liquidation scenarios

### 3. Multi-Entry Positions
- Average price calculation with multiple entries
- Complex trading scenarios with DCA

### 4. Risk Assessment
- Position health evaluation
- Margin utilization warnings
- Liquidation proximity alerts

## Expected vs Actual Validation

The test suite compares calculated values against expected mathematical results with tolerance for floating-point precision:

```
📋 Calculation Results:
Metric               Expected        Actual          Match
------------------------------------------------------------
position_value       $5500.00        $5500.00        ✅
margin_used          550.000000     550.000000     ✅
unrealized_pnl       $500.00         $500.00         ✅
liquidation_price    $45050.00       $45050.00       ✅
liquidation_ratio    0.8191         0.8191         ✅
```

## Position Summary Output

The calculator provides a comprehensive position summary:

```
📋 Position Summary for BTCUSDT:
   💰 Account Balance: $10,000.00
   📈 Position Size: 0.1
   💵 Position Value: $5,500.00
   🏦 Margin Required: $550.00 (5.50% of balance)
   📊 Unrealized P&L: $500.00
   ⚡ ROE: +90.91%
   🚨 Liquidation Price: $45,050.00
   📏 Distance to Liquidation: 18.09%

🎯 Risk Assessment:
   Position Status: ✅ Healthy
   P&L Status: Profit
   Margin Utilization: 5.50%
```

## Validation Notes

- All calculations follow Bybit's USDT perpetual futures methodology
- Cross-validated between legacy `Position` class and new `PositionTracker` system
- Includes realistic commission and slippage considerations
- Handles edge cases and extreme market conditions
- Provides warnings for high-risk positions

## Usage Tips

1. **For Testing Strategies**: Use the comprehensive test suite to validate your position management logic
2. **For Quick Calculations**: Use the interactive calculator for specific scenarios
3. **For Integration**: Import `calculate_user_scenario` function for programmatic use
4. **For Risk Management**: Pay attention to liquidation distance and margin utilization warnings