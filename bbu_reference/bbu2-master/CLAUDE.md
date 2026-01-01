# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

**Dependency Management:**
```bash
# Install dependencies
uv sync

# Activate virtual environment
source .venv/bin/activate
```

**Running the Application:**
```bash
# Run the main trading bot
uv run main.py
```

**Testing:**
```bash
# Run specific tests (no unified test framework configured)
python test_position_low_margin.py
```

## Git Workflow Guidelines

**IMPORTANT: Always follow this workflow when implementing new features:**

### Before Starting Any New Feature Implementation

1. **Always ask first:**
   - "Should I commit and push current changes to [current-branch]?"
   - "Should I create a new branch for this feature?"
   - "Suggested branch name: feature/[descriptive-name]"
   - **Wait for explicit confirmation before proceeding**

2. **Upon confirmation, execute in this order:**
   - Commit current uncommitted changes with descriptive message
   - Push current branch to remote repository
   - Create new feature branch with agreed name
   - Checkout the new feature branch
   - Begin implementing the requested feature

### Benefits of This Workflow

- **Clean separation** of features and functionality
- **Easy review** and merge process for individual features
- **Better rollback** capabilities if issues arise
- **Professional git history** with focused commits
- **Reduced risk** of mixing unrelated changes

### Branch Naming Convention

- `feature/[descriptive-name]` - For new functionality
- `bugfix/[issue-description]` - For bug fixes
- `refactor/[component-name]` - For code improvements

**This workflow applies to ALL feature requests across ANY project and should be followed consistently.**

## Project Architecture

BBU2 is a cryptocurrency trading bot with multi-exchange support and Telegram integration.

### Core Components

**Main Entry Point:**
- `main.py` - Application entry point that initializes the Controller
- `controller.py` - Main bot controller that manages strategies and exchange connections

**Exchange APIs:**
- `bybit_api_usdt.py` - Primary Bybit USDT trading API (actively used)
- `bybit_api.py` - Legacy Bybit API implementation
- `bitmex_api.py` - BitMEX trading API implementation

**Strategy System:**
- `strat.py` - Contains trading strategies (Strat50 is the main strategy)
- Strategies are configured per symbol in `conf/config.yaml` with parameters like:
  - `greed_count`, `greed_step` - Position sizing logic
  - `max_margin`, `min_total_margin` - Risk management
  - `long_koef` - Long/short bias multiplier

**Configuration:**
- `settings.py` - Centralized settings loader with fallback paths
- `conf/config.yaml` - Trading strategy configurations
- `conf/keys.yaml` - API keys and credentials (not in repo)
- `conf/server_config.yaml` - Server/notification settings

**Supporting Modules:**
- `position.py` - Position management logic
- `telega.py` - Telegram bot integration
- `TelegramExcBot.py` - Exception reporting via Telegram
- `loggers.py` - Comprehensive logging system
- `db_files.py` - File-based data persistence
- `greed.py` - Greed-based position sizing algorithm
- `calc.py` - Trading calculations and utilities

### Data Flow

1. Controller initializes exchange connections and strategies based on config
2. Each strategy monitors specific symbol/timeframe pairs
3. Strategies execute trades through exchange API wrappers
4. Position management tracks open positions and risk
5. Comprehensive logging captures all activities
6. Telegram integration provides real-time notifications

### Configuration Structure

The bot supports multiple exchange accounts with individual configuration:
- Each account has API keys, trading amounts, and strategy assignments
- Strategies are mapped to specific symbols with custom parameters
- Testnet mode available for development/testing

### Key Files to Understand for Development

- `controller.py:16-36` - Strategy initialization and mapping
- `controller.py:38-70` - Exchange connection setup
- `settings.py:13-15` - Configuration file search paths
- `bybit_api_usdt.py` - Primary trading implementation
- `strat.py` - Trading logic and signal generation