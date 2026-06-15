# Grid Bot Validation

A monorepo for running, recording, and validating a Bybit USDT-perpetual grid trading bot.

It contains the live trading bot, an event-driven backtester, a mainnet data recorder, a replay engine that drives the bot against recorded data, and tooling to compare backtest results against live execution.

## Repository Layout

```
grid-bot-validation/
├── packages/                # Reusable libraries (pure logic, no I/O at the edges)
│   ├── gridcore/            # Grid strategy engine — zero external dependencies
│   └── bybit_adapter/       # Bybit REST + WebSocket client wrappers
│
├── shared/
│   └── db/                  # grid-db — SQLAlchemy models, multi-tenant DB layer
│
├── apps/                    # Runnable services / CLIs
│   ├── gridbot/             # Live multi-tenant grid trading bot
│   ├── backtest/            # Historical backtest engine
│   ├── recorder/            # Captures live Bybit market + private data to SQLite
│   ├── replay/              # Replays recorded data through the strategy engine
│   ├── comparator/          # Diffs backtest vs live trade outcomes
│   ├── event_saver/         # Streaming event persistence used by gridbot
│   └── pnl_checker/         # Reconciles our PnL math against Bybit's API
│
├── conf/                    # Shared on-disk caches (risk-limit tiers, instruments)
├── docs/                    # Architecture notes, feature plans, validation results
├── scripts/                 # One-off migrations and analysis scripts
├── tests/integration/       # Cross-package integration tests
├── Makefile                 # `make test`, `make lint`, `make clear-log`
├── pyproject.toml           # uv workspace root
└── uv.lock
```

The workspace is glued together by `uv` (`[tool.uv.workspace]` in the root `pyproject.toml`). Every directory under `packages/`, `shared/`, and `apps/` is a workspace member resolved from source — there is nothing to publish.

## Components

### Libraries

**`gridcore`** — Pure grid trading logic with **zero external dependencies**.
Holds grid-level math (`greed.py`), the event-driven strategy engine (`strat.py`), per-position risk management (`position.py`), and the maintenance-margin tier tables / margin formulas used by every downstream component. Everything else depends on it.

**`bybit_adapter`** — Thin wrappers around `pybit` for Bybit V5 REST + WebSocket APIs (public market data, private order/position/wallet streams, risk-limit lookups). Keeps exchange-specific concerns out of `gridcore`.

**`grid-db`** — SQLAlchemy 2.0 data layer shared by every app. Supports SQLite (development, replay, recorder) and PostgreSQL (production gridbot). Tables cover orders, fills, positions, wallet snapshots, ticker snapshots, and per-strategy runs.

### Apps

**`gridbot`** — The live multi-tenant grid trading bot. Reads a YAML config of accounts + strategies, attaches `gridcore` engines to live Bybit streams, places/cancels orders, and persists every state transition via `grid-db`. Supports hedge mode, dynamic risk multipliers, and Telegram notifications.

**`backtest`** — Offline historical backtester. Drives `gridcore` against recorded ticker snapshots and fills with a configurable fill simulator (`book_touch`, `trade_through_at_limit`, `strict_cross`, `last_cross`), accurate maintenance-margin tiers, funding accrual, and an honest hedge-aware pair-liquidation model. Outputs per-strategy reports (PnL curve, drawdown, fill log).

**`recorder`** — A standalone process that subscribes to Bybit mainnet WebSocket streams and writes them to SQLite for later replay. Captures L1 ticker snapshots, public trades (optional), and — when API keys are provided — private orders, executions, positions, and wallet snapshots. Tracks gaps and reconciles via REST.

**`replay`** — Reads a recorder database for a given `run_id` and time window, then feeds it through the same `gridcore` engine the live bot uses. The point is *shadow validation*: did our strategy, given the exact market it saw live, produce the same orders, fills, and PnL? Hands its output to `comparator`.

**`comparator`** — Compares a replay/backtest run against the corresponding live run from the same database. Surfaces order divergences, fill mismatches, and PnL deltas with configurable price/quantity tolerances.

**`event_saver`** — Background writer used inside `gridbot` to batch streaming events into the database without blocking the trading loop.

**`pnl_checker`** — CLI that pulls live wallet/position state from Bybit and compares it against `gridcore`'s own PnL calculations. Used to keep our margin/liquidation math honest against the real exchange.

## Data Flow

```
                ┌──────────────────────────────┐
                │ Bybit Mainnet (WS + REST)    │
                └──────────┬───────────────────┘
                           │
        ┌──────────────────┼──────────────────────────┐
        │                  │                          │
        ▼                  ▼                          ▼
   ┌─────────┐        ┌─────────┐               ┌────────────┐
   │ gridbot │───────▶│ grid-db │◀──────────────│  recorder  │
   └─────────┘  live  └────┬────┘    recorded   └────────────┘
        ▲                  │
        │                  │ replay reads same DB
        │                  ▼
        │            ┌──────────┐      ┌────────────┐
        │            │  replay  │─────▶│ comparator │
        │            └──────────┘      └────────────┘
        │                                    │
        │                                    ▼
        │                              live-vs-shadow diff
        │
   ┌────┴────────┐
   │ pnl_checker │  (sanity-checks live PnL math vs Bybit)
   └─────────────┘
```

`gridcore` is the strategy engine shared by **gridbot**, **backtest**, and **replay** — that is the entire point of the architecture. The same code that trades live also runs against recorded history and against synthetic backtest data, so divergences are bugs in I/O wrappers or in the data, not in two parallel strategy implementations.

## Quick Start

### Prerequisites
- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) for dependency management
- SQLite (bundled) for local dev; PostgreSQL for production gridbot

### Install

```bash
git clone <repository-url>
cd grid-bot-validation
uv sync
```

`uv sync` resolves the workspace and installs every package in editable mode. No further `pip install` is needed.

### Configure

Each app reads YAML from its own `conf/` directory. Copy the `.example` file and edit:

```bash
cp apps/gridbot/conf/gridbot.yaml.example   apps/gridbot/conf/gridbot.yaml
cp apps/backtest/conf/backtest.yaml.example apps/backtest/conf/backtest.yaml
cp apps/recorder/conf/recorder.yaml.example apps/recorder/conf/recorder.yaml
cp apps/replay/conf/replay.yaml.example     apps/replay/conf/replay.yaml
```

API keys and DB URLs are referenced via environment variables (`${BYBIT_API_KEY}`, `${DATABASE_URL}`) — do **not** commit secrets. Recorder configs that hold credentials should be `chmod 600`.

### Run

```bash
# Live trading bot
uv run python -m gridbot.main --config apps/gridbot/conf/gridbot.yaml

# Backtest
uv run python -m backtest --config apps/backtest/conf/backtest.yaml

# Recorder (standalone process — keep running to capture data)
uv run python -m recorder.main --config apps/recorder/conf/recorder.yaml

# Replay recorded data through the strategy engine
uv run python -m replay.main --config apps/replay/conf/replay.yaml

# Compare backtest/replay vs live
uv run python -m comparator --config apps/replay/conf/replay.yaml

# Validate live PnL against Bybit
uv run python -m pnl_checker --config apps/pnl_checker/conf/pnl_checker.yaml
```

## Configuration Cheat Sheet

A typical gridbot strategy block:

```yaml
strategies:
  - strat_id: ltcusdt_main
    account: mainnet_live
    symbol: LTCUSDT
    tick_size: "0.1"
    grid_count: 20
    grid_step: 0.3
    amount: "x0.001"             # 0.1% of wallet per order
    max_margin: 5.0              # hard cap on margin used
    min_total_margin: 3          # threshold for the low-margin boost
    increase_same_position_on_low_margin: true
    shadow_mode: false           # true = log-only, no orders sent
```

The same shape (minus `account` / `shadow_mode`) appears in `backtest.yaml`, so a strategy can be backtested and live-traded from near-identical config.

## Development

### Tests

```bash
# Full suite (per-package, with coverage merged)
make test

# Cross-package integration tests only
make test-integration

# Single package
uv run pytest packages/gridcore/tests --cov=gridcore -v

# Single file
uv run pytest apps/backtest/tests/test_runner.py -v
```

Always run tests through `uv run` — bare `python -m pytest` skips the workspace's editable installs and resolves the wrong import paths.

### Lint

```bash
make lint   # ruff over the whole workspace
```

### Continuous Integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `make test` and
`make lint` (as separate jobs) on every pull request and on every push to
`main`. **CI green is the source of truth for repo health** — reproduce any
failure locally with the same `make` target. Both jobs fail on any non-zero
exit; while the repo is being made green (issues #177–#180) the check is not
yet required for merge in branch protection.

### Adding a Dependency

```bash
# To a specific package
cd packages/<name>
uv add <package>

# To workspace dev tools
uv add --dev <package>
```

## Database

`grid-db` works with both SQLite and PostgreSQL via SQLAlchemy. For local development and replay, point `database_url` at a SQLite file:

```yaml
database_url: "sqlite:///gridbot.db"
```

For production gridbot, use PostgreSQL:

```yaml
database_url: "${DATABASE_URL}"   # postgresql+psycopg2://...
```

Multi-tenancy is enforced at the row level via `account` + `strat_id` columns — one database can host any number of strategies and accounts side by side.

## Workflow

Project conventions live in `CLAUDE.md` (development workflow) and `RULES.md` (code-style / domain gotchas). Feature plans, post-mortems, and architecture notes live under `docs/`.

The short version: clearly define the task, search the codebase and `RULES.md` for prior art, agree on a plan before touching code, implement against tests, then update `RULES.md` with anything that future-you would want to know.