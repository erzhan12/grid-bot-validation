# Grid Bot Backtest System - Usage Guide

## 🎯 Quick Start

Your backtest system is now ready! Here's how to use it with your actual historical data:

### 1. Basic Backtest (Recommended)

```bash
# Run backtest for BTCUSDT with default settings
python run_backtest.py BTCUSDT

# Run backtest for ETHUSDT with custom balance
python run_backtest.py ETHUSDT --balance 50000

# Run backtest with results export
python run_backtest.py BTCUSDT --export
```

### 2. Demo Mode (Test Without Database)

```bash
# See how the system works with simulated data
python demo_backtest.py
```

## 📊 What You'll Get

### Real-Time Output
```
🚀 Starting Grid Bot Backtest
Symbol: BTCUSDT
Initial Balance: $10,000.00
==================================================
✅ Loading backtest system...
✅ Created session: PROD_BTCUSDT_20250912_153000
✅ Initializing controller for BTCUSDT...
✅ Setting up 1 market makers for backtesting...
   📊 vezun_bb_btc initialized with amount x0.003
✅ Initialized 1 strategies
   🎯 Strategy Strat50 (ID: 3)
      Greed: 20 levels, 0.5% step

🔄 Starting backtest execution...
   This will process all historical data for BTCUSDT
```

### Comprehensive Results
```
📊 BACKTEST RESULTS
==================================================
💰 Financial Performance:
   Initial Balance: $10,000.00
   Final Balance:   $10,245.67
   Total PnL:       $+245.67
   Return:          +2.46%

📈 Trading Statistics:
   Total Trades:    147
   Winning Trades:  89
   Win Rate:        60.5%

🎯 Detailed Metrics for BTCUSDT:
   Max Drawdown:    1.23%
   Max Profit:      $312.45
   Profit Factor:   1.67

🎯 Order Statistics:
   Orders Created:  1,234
   Orders Filled:   147
   Fill Rate:       11.9%
   Slippage:        5 bps
```

## 🔧 System Configuration

### Your Current Config (config/config.yaml)
The system uses your existing configuration:

```yaml
pair_timeframes:
  - id: 3
    strat: Strat50
    symbol: BTCUSDT
    greed_count: 20      # Grid levels
    greed_step: 0.5      # 0.5% between levels
    long_koef: 1.0       # Position sizing
    min_total_margin: 2

amounts:
  - name: vezun_bb_btc
    amount: x0.003       # Order size
    strat: 3
```

### Backtest Settings (Configurable)
- **Initial Balance**: Default $10,000 (configurable)
- **Slippage**: 5 basis points (0.05%)
- **Commission**: 6 basis points (0.06%)
- **Data Source**: Your `ticker_data` table

## 📁 Results Export

When you use `--export`, you get detailed CSV files:

```
📁 Results exported to: ./backtest_results/BTCUSDT_PROD_BTCUSDT_20250912_153000/
   ├── trades_PROD_BTCUSDT_20250912_153000.csv       # All executed trades
   ├── positions_PROD_BTCUSDT_20250912_153000.csv    # Position snapshots
   └── summary_PROD_BTCUSDT_20250912_153000.csv      # Performance summary
```

### Trades CSV Format
```csv
Trade ID,Symbol,Side,Size,Price,Direction,Executed At,Order ID,Strategy ID,BM Name,Realized PnL
TRADE_000001,BTCUSDT,buy,0.003,49500.0,long,2024-01-15 10:30:15,ORDER_000123,3,vezun_bb_btc,-0.09
```

### Positions CSV Format
```csv
Timestamp,Symbol,Direction,Size,Entry Price,Current Price,Unrealized PnL,Margin,Liquidation Price
2024-01-15 10:30:15,BTCUSDT,long,0.003,49500.0,50000.0,1.5,148.5,44550.0
```

## 🎯 Understanding the Results

### Key Metrics Explained

1. **Total Return**: Overall profit/loss percentage
2. **Win Rate**: Percentage of profitable trades
3. **Profit Factor**: Gross profit ÷ Gross loss (>1 is good)
4. **Max Drawdown**: Largest peak-to-trough decline
5. **Fill Rate**: Percentage of orders that got filled

### Grid Strategy Analysis

The backtest shows how your grid bot performs:
- **Order Placement**: Creates buy/sell orders in a grid pattern
- **Order Fills**: Realistic fills when price crosses grid levels
- **Position Management**: Tracks long/short positions separately
- **Rebalancing**: Recreates grid as orders fill

## 🚀 Running Your First Backtest

### Step 1: Choose a Symbol
Pick a symbol from your config.yaml:
- BTCUSDT (greed_step: 0.5%)
- ETHUSDT (greed_step: 0.45%)
- LTCUSDT (greed_step: 0.5%)
- SOLUSDT (greed_step: 0.8%)

### Step 2: Run the Backtest
```bash
python run_backtest.py BTCUSDT --export
```

### Step 3: Analyze Results
1. Review the console output for key metrics
2. Check the exported CSV files for detailed analysis
3. Import CSV data into Excel/Python for further analysis

## 🛠️ Advanced Usage

### Custom Analysis Script
```python
from src.backtest_session import BacktestSession
from src.backtest_reporter import BacktestReporter

# Load a completed backtest session
# (You'll need to save/load sessions for this)
session = BacktestSession("YOUR_SESSION_ID")

# Generate detailed reports
reporter = BacktestReporter(session)
reporter.print_detailed_report()
reporter.export_to_csv("./my_results")
```

### Multiple Symbol Comparison
```bash
# Run backtests for all symbols
python run_backtest.py BTCUSDT --export
python run_backtest.py ETHUSDT --export
python run_backtest.py LTCUSDT --export
python run_backtest.py SOLUSDT --export
```

## 📈 Optimization Ideas

Based on your backtest results, you can optimize:

1. **Grid Spacing** (greed_step): Tighter grids = more trades
2. **Grid Levels** (greed_count): More levels = broader coverage
3. **Order Size** (amount): Larger orders = bigger profits/losses
4. **Position Ratios**: Adjust long/short balance

## 🔍 Troubleshooting

### Common Issues

1. **"No module named 'yaml'"**
   ```bash
   pip install pyyaml
   ```

2. **"No module named 'psycopg2'"**
   ```bash
   pip install psycopg2-binary
   ```

3. **Database Connection Error**
   - Check your DATABASE_URL environment variable
   - Ensure your ticker_data table has data for the symbol

4. **No Trades Generated**
   - Check if you have historical data for the symbol
   - Verify grid levels aren't too wide for the price movement

### Debug Mode
```bash
# Run with verbose output for debugging
python run_backtest.py BTCUSDT --verbose
```

## 📊 Performance Tips

1. **Start Small**: Test with 1-2 days of data first
2. **Check Fill Rates**: Low fill rates may indicate grid levels are too narrow
3. **Monitor Drawdown**: High drawdown suggests too much risk
4. **Compare Symbols**: Different symbols have different optimal grid settings

## 🎉 Success Metrics

Your grid bot is performing well if:
- **Fill Rate**: 10-30% (depends on market volatility)
- **Win Rate**: 50-70% (grid strategies profit from oscillation)
- **Profit Factor**: >1.2 (indicates more profit than loss)
- **Max Drawdown**: <5% (manageable risk)

## 📞 Next Steps

1. **Run Your First Backtest**: `python run_backtest.py BTCUSDT --export`
2. **Analyze Results**: Review metrics and exported data
3. **Compare Strategies**: Test different symbols and settings
4. **Optimize**: Adjust grid parameters based on results
5. **Deploy**: Use insights to configure your live grid bot

---

🎯 **Your grid bot backtest system is ready for production use!**

The system processes your historical ticker data exactly like your live bot would, giving you realistic performance projections and strategy insights.
