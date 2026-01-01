# BBU Backtest - Product Brief

## Project Overview

BBU Backtest is a sophisticated cryptocurrency trading strategy backtesting system designed specifically for grid trading bots. The system enables traders to test and validate their grid trading strategies using historical market data before deploying them in live markets. Built with Python and leveraging PostgreSQL for historical data storage, it provides realistic simulation of trading conditions including slippage, commissions, and order fills.

## Target Audience

- **Cryptocurrency Traders**: Individual and institutional traders looking to validate grid trading strategies
- **Algorithmic Trading Developers**: Developers building automated trading systems who need robust backtesting capabilities
- **Quantitative Analysts**: Financial analysts requiring detailed performance metrics and risk analysis
- **Trading Strategy Researchers**: Researchers studying market behavior and strategy effectiveness

## Primary Benefits / Features

### Core Trading Capabilities
- **Grid Trading Strategy**: Implements sophisticated grid trading with configurable levels, step sizes, and rebalancing
- **Multi-Symbol Support**: Backtest across multiple cryptocurrency pairs (BTCUSDT, ETHUSDT, LTCUSDT, SOLUSDT)
- **Realistic Simulation**: Includes market-realistic slippage (5 bps), commissions (6 bps), and order fill logic
- **Position Management**: Comprehensive long/short position tracking with PnL calculations and liquidation price monitoring

### Backtesting Engine
- **Historical Data Integration**: Seamlessly processes historical ticker data from PostgreSQL database
- **In-Memory Processing**: All backtest data stored in memory for fast execution and analysis
- **Real-Time Progress Tracking**: Live updates during backtest execution with detailed progress reporting
- **Flexible Configuration**: YAML-based configuration for easy strategy parameter adjustment

### Performance Analysis
- **Comprehensive Metrics**: Win rate, profit factor, drawdown analysis, and return calculations
- **Detailed Reporting**: CSV export of trades, positions, and performance summaries
- **Strategy Comparison**: Built-in comparison with buy-and-hold strategies
- **Risk Assessment**: Margin usage, liquidation risk, and position sizing analysis

### Developer Experience
- **Production Ready**: Complete implementation with comprehensive testing and documentation
- **Modular Architecture**: Clean separation of concerns with strategy, order management, and data layers
- **Easy Deployment**: Simple command-line interface for running backtests
- **Extensible Design**: Plugin architecture for adding new strategies and exchanges

## High-Level Tech/Architecture

### Technology Stack
- **Backend**: Python 3.x with asyncio support
- **Database**: PostgreSQL for historical price data storage
- **Configuration**: YAML-based configuration management
- **Dependencies**: psycopg2-binary, pyyaml, pybit for exchange integration

### System Architecture
- **Controller Layer**: Main orchestrator managing strategies and market makers
- **Strategy Engine**: Pluggable strategy system with Strat50 grid trading implementation
- **Order Management**: Complete order lifecycle with realistic fill simulation
- **Data Provider**: Database abstraction layer for historical data iteration
- **Backtest Session**: In-memory storage for trades, positions, and metrics
- **Reporting Engine**: Comprehensive analysis and export capabilities

### Key Components
- **BacktestEngine**: Main orchestrator for running backtests
- **Strat50**: Grid trading strategy with dynamic rebalancing
- **BybitApiUsdt**: Exchange simulation with dual-mode support (live/backtest)
- **BacktestOrderManager**: Order management with slippage and fill logic
- **BacktestSession**: In-memory data storage and metrics calculation
- **Greed System**: Grid generation and rebalancing logic

### Data Flow
1. Historical price data loaded from PostgreSQL ticker_data table
2. Strategy processes each price tick through grid logic
3. Orders created, filled, and managed with realistic simulation
4. Positions tracked with real-time PnL calculations
5. Results stored in memory and exported for analysis

The system is designed for production use with comprehensive error handling, logging, and performance optimization for processing large historical datasets efficiently.
