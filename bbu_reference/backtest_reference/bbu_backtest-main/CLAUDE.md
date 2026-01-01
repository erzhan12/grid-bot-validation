# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BBU Backtest is a cryptocurrency trading strategy backtesting system designed for grid trading bots. It simulates trading on Bybit exchange using historical data stored in PostgreSQL, with realistic slippage, commissions, and order fills.

## Development Commands

### Testing
```bash
make test                               # Run all tests
pytest tests/                           # Alternative test command
pytest --cov                           # Run tests with coverage
pytest tests/test_specific_file.py      # Run single test file
```

### Code Quality
```bash
make lint          # Run ruff linter
ruff check .       # Alternative lint command
```

### Running Backtests
```bash
python main.py --symbol BTCUSDT --balance 50000 --export            # Run backtest with CLI options
python main.py --symbol ETHUSDT --quiet                             # Run quiet backtest
python main.py --symbol LTCUSDT --start_datetime "2025-09-18 13:05:49"  # Start from specific datetime
python demo_backtest.py                                             # Run demo with simulated data
make runbtc                                                         # Shortcut for BTCUSDT backtest
```

### Environment Setup
```bash
uv sync            # Install dependencies
```

## Architecture Overview

### Core Components

**Controller (`src/controller.py`)**: Main orchestrator that manages strategies and market makers. Initializes trading pairs from `config/config.yaml` and coordinates between strategies and exchange APIs.

**Strat50 (`src/strat.py`)**: Grid trading strategy implementation. Processes historical price data from `DataProvider` one tick at a time, managing grid order placement using the Greed system for dynamic rebalancing. The `_check_pair_step()` method drives the backtest iteration loop.

**BybitApiUsdt (`src/bybit_api_usdt.py`)**: Dual-mode exchange interface supporting both live trading and backtesting. In backtest mode, simulates order fills with realistic slippage (5 bps) and commissions (2 bps for maker/limit orders).

**BacktestRunner (`src/backtest_runner.py`)**: Core backtesting functionality that executes backtests with historical data and generates comprehensive results.

**BacktestEngine (`src/backtest_engine.py`)**: Main orchestrator for running backtests. Leverages existing architecture to provide realistic simulation.

**BacktestOrderManager (`src/backtest_order_manager.py`)**: Handles order lifecycle in backtest mode with realistic fill simulation and position tracking.

**BacktestSession (`src/backtest_session.py`)**: In-memory storage for trades, positions, and metrics during backtest execution.

**Greed System (`src/greed.py`)**: Grid generation and rebalancing logic that calculates optimal grid levels and step sizes.

**Position Management**: Production-grade position tracking with PnL calculations, liquidation price monitoring, and comprehensive position analytics in `src/position.py` and `src/position_tracker.py`.

### Data Flow

1. Historical price data loaded from PostgreSQL `ticker_data` table via `DataProvider`
2. Controller initializes strategies based on `config/config.yaml` settings
3. Strat50 processes each price tick through grid logic
4. Orders created and managed through BacktestOrderManager with realistic simulation
5. Positions tracked with real-time PnL calculations
6. Results stored in BacktestSession and exported for analysis

### Configuration

**Primary Config**: `config/config.yaml` defines trading pairs, strategy parameters (greed_count, greed_step), and account amounts. Each pair_timeframe entry must have a matching account in the amounts section.

**Settings**: `config/settings.py` provides Pydantic-based configuration management with environment variable support. Database connection configured via `DATABASE_URL` or individual DB_* environment variables.

## Key Files for Understanding

- `main.py`: CLI entry point with ArgumentParser for backtest execution
- `src/backtest_runner.py`: Core backtesting logic and result reporting
- `src/controller.py`: Strategy and market maker coordination
- `src/strat.py`: Grid trading strategy implementations (focus on Strat50)
- `src/bybit_api_usdt.py`: Exchange simulation and order management
- `src/backtest_engine.py`: Main backtesting orchestrator
- `src/greed.py`: Grid calculation and rebalancing logic
- `config/config.yaml`: Trading pair and strategy configuration

## Testing Strategy

Tests are comprehensive and cover:
- Position management and PnL calculations
- Order lifecycle and fill simulation
- Strategy logic and grid generation
- Database connections and data providers
- Backtest engine integration

Run single test files with: `pytest tests/test_specific_file.py -v` for verbose output

## Code Quality Standards

- Line length: 140 characters (configured in `ruff.toml`)
- Linting with ruff (select: E, F, I, C90)
- Type hints encouraged for new code
- Comprehensive test coverage expected

## Database Schema

The backtest system reads from the `ticker_data` table in PostgreSQL:
- Primary key: `id` (auto-increment)
- Columns: `symbol`, `timestamp`, `lastPrice`, and other OHLCV data
- Data accessed via cursor-based pagination through `DataProvider`
- Supports filtering by `start_datetime` for partial backtests
