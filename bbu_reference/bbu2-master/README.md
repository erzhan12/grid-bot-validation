# BBU2 - Trading Bot

A Python-based trading bot for cryptocurrency exchanges (Bybit, BitMEX) with Telegram integration.

## Setup

This project uses [UV](https://github.com/astral-sh/uv) for dependency management.

### Prerequisites
- Python 3.12+
- UV package manager

### Installation
```bash
# Clone the repository
git clone <repository-url>
cd bbu2

# Install dependencies using UV
uv sync

# Activate virtual environment
source .venv/bin/activate  # On macOS/Linux
# or
.venv\Scripts\activate     # On Windows
```

## Configuration

1. Copy and configure your API keys in `conf/keys.yaml`
2. Adjust settings in `conf/config.yaml`
3. Set up Telegram bot configuration if needed

## Usage

```bash
# Run the main bot
python main.py

```

## Features

- Multi-exchange support (Bybit, BitMEX)
- Telegram bot integration
- Position management
- Automated trading strategies
- Comprehensive logging system

## Project Structure

- `main.py` - Entry point
- `controller.py` - Main bot controller
- `bybit_api.py` / `bybit_api_usdt.py` - Bybit exchange integration
- `bitmex_api.py` - BitMEX exchange integration
- `telega.py` - Telegram bot functionality
- `conf/` - Configuration files
- `logs/` - Application logs
