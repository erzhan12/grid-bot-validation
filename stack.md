# Stack

Python 3.11+ 
monorepo managed with `uv` (workspace mode, no Node). 
Exchange: **Bybit** (V5 API, USDT-perpetuals on mainnet) via `pybit` for REST/WebSocket. 
Persistence through `SQLAlchemy 2.0` — SQLite for local/replay/recorder, PostgreSQL (`psycopg2-binary`) for production gridbot. 
Configuration with `pydantic` v2 + `pydantic-settings` + `pyyaml`, secrets via `python-dotenv`. 
Telegram notifications through `pytelegrambotapi`, CLI rendering via `rich`. 
Dev tooling: `pytest` (+ `pytest-asyncio`, `pytest-cov`) and `ruff`. 
AI pair-programmer: **Claude Code** (Anthropic), driven by `CLAUDE.md` / `RULES.md` / `docs/features/*` plans in the repo.