# pnl_checker

Live PnL validation tool — compares our calculations against Bybit's reported values.

## What it does

1. **Fetches** live position, ticker, wallet, and funding data from Bybit REST API
2. **Calculates** PnL, margin, and risk multipliers using our formulas (gridcore)
3. **Compares** our values against Bybit's with configurable tolerance
4. **Reports** results to console (rich tables) and JSON file

## Setup

### 1. Install dependencies

From the repo root:

```bash
uv sync
```

### 2. Configure credentials

Copy the example config and add your API credentials:

```bash
cp conf/pnl_checker.yaml.example conf/pnl_checker.yaml
```

Edit `conf/pnl_checker.yaml` with your Bybit API key and secret. Alternatively, set environment variables:

```bash
export BYBIT_API_KEY="your_key"
export BYBIT_API_SECRET="your_secret"
```

Environment variables take precedence over config file values.

### 3. Configure symbols

Edit `conf/pnl_checker.yaml` to list the symbols you want to validate:

```yaml
symbols:
  - symbol: "BTCUSDT"
    tick_size: "0.1"
  - symbol: "ETHUSDT"
    tick_size: "0.01"
```

## Usage

```bash
# Basic run (uses conf/pnl_checker.yaml)
uv run python -m pnl_checker.main

# Specify config path
uv run python -m pnl_checker.main --config conf/pnl_checker.yaml

# Override tolerance (in USDT)
uv run python -m pnl_checker.main --tolerance 0.001

# Debug logging
uv run python -m pnl_checker.main --debug

# Custom output directory
uv run python -m pnl_checker.main --output results/
```

### Exit codes

- `0` — All checks passed
- `1` — One or more checks failed, or a fatal error occurred

## Output interpretation

### Console output

The tool prints a rich table per position showing:

| Column | Meaning |
|--------|---------|
| Field | What's being compared |
| Bybit | Value reported by Bybit API |
| Ours | Value computed by our formulas |
| Delta | Absolute difference |
| Status | PASS/FAIL/INFO |

**Checked fields** (with tolerance): Unrealized PnL, Position Value, Initial Margin, PnL % ROE

**Informational fields** (no check): Entry price, mark price, leverage, liquidation, funding, risk multipliers

### JSON output

Results are saved to `output/pnl_check_<timestamp>.json` containing:
- Per-position comparisons with all field values
- Account-level wallet summary
- Redacted config (credentials replaced with `[REDACTED]`)

## Configuration reference

| Field | Default | Description |
|-------|---------|-------------|
| `account.api_key` | — | Bybit API key (or `BYBIT_API_KEY` env var) |
| `account.api_secret` | — | Bybit API secret (or `BYBIT_API_SECRET` env var) |
| `symbols` | — | List of symbols with tick_size |
| `risk_params.min_liq_ratio` | 0.8 | Minimum liquidation ratio |
| `risk_params.max_liq_ratio` | 1.2 | Maximum liquidation ratio |
| `risk_params.max_margin` | 8.0 | Maximum margin per position |
| `risk_params.min_total_margin` | 0.15 | Minimum total margin |
| `funding_max_pages` | 20 | Max pages for funding tx log (50 records/page) |
| `tolerance` | 0.01 | USDT tolerance for pass/fail |

## Running tests

```bash
# Run pnl_checker tests
uv run pytest apps/pnl_checker/tests -v

# With coverage
uv run pytest apps/pnl_checker/tests --cov=pnl_checker --cov-report=term-missing -v
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Config file not found` | Set `PNL_CHECKER_CONFIG_PATH` env var or create `conf/pnl_checker.yaml` |
| `API credentials required` | Add credentials to config file or set `BYBIT_API_KEY`/`BYBIT_API_SECRET` |
| `Network error` | Check internet connection and Bybit API status |
| `No open positions found` | Verify you have open positions for the configured symbols |
| `Funding Data Truncated` | Increase `funding_max_pages` in config |
| Large PnL deltas | Check that `tick_size` matches the exchange's actual tick size |
