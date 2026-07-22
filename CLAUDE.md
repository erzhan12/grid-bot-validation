# Project
Monorepo to run, record, and validate a Bybit USDT-perp **grid trading bot** — live bot, event-driven backtester, mainnet recorder, replay engine, and a backtest-vs-live comparator.

**Stack:** Python ≥3.11, `uv` workspace, pytest + pytest-asyncio, ruff, SQLAlchemy/SQLite.

**Layout** (full tree + component docs in `README.md`):
- `packages/gridcore` — grid strategy engine, zero external deps (core logic)
- `packages/bybit_adapter` — Bybit REST + WS client wrappers
- `shared/db` — `grid-db`, SQLAlchemy multi-tenant models
- `apps/*` — runnable CLIs: `gridbot` (live), `backtest`, `recorder`, `replay`, `comparator`, `event_saver`, `pnl_checker`

**Entry points:** each app at `apps/<name>/src/<name>/main.py`; run via `uv run <name>`.

# Commands
- `make test` — all tests (runs each package separately to avoid conftest conflicts)
- `make test-integration` — integration tests only
- `make lint` — ruff (line-length 88)
- `make live-check` — replay-vs-live reconciliation
- `uv run pytest packages/<name>` — single package; always via `uv run`, never bare `pytest` / `python -m pytest`
- `uv run <app>` — run an app (e.g. `uv run gridbot`)

# Coding principles
- See `.claude/rules/code-style.md` for coding principles (simplicity first, surgical changes, plan-first, goal-driven execution) and safety rules.
- Project rules live in `.claude/rules/` (feature 0096): component rules are path-scoped and auto-load when matching files are read; `RULES.md` is only the index. Workflow step "update RULES.md" = edit the relevant `.claude/rules/*.md` + keep the index in sync.
