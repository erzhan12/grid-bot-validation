---
paths:
  - "shared/db/**"
---

## grid_db — Multi-Tenant Database Layer

**Path**: `shared/db/` | **Tables**: users, bybit_accounts, api_credentials, strategies, runs, public_trades, private_executions, plus position/wallet snapshots and orders

### Key Rules

- **CRITICAL**: All queries MUST filter by `user_id` for data isolation
- `BaseRepository` does NOT expose `get_by_id`/`get_all` (removed for safety)
- Use `String(36)` for UUIDs, `BigInteger().with_variant(Integer, "sqlite")` for high-volume PKs
- SQLite: requires `PRAGMA foreign_keys=ON` on every connection; `StaticPool` ONLY for `:memory:`
- PostgreSQL URL encoding: use `urllib.parse.quote_plus()` for connection components (not port)
- All FKs have `ondelete="CASCADE"` + ORM `cascade="all, delete-orphan"`
- Use `DatabaseFactory.get_session()` context manager for auto commit/rollback
- Bulk inserts use `ON CONFLICT DO NOTHING` (trades/executions) or `ON CONFLICT DO UPDATE` (orders)
- `redact_db_url()` from `grid_db.utils` — **always** use when logging DB URLs
- **Repository module layout (feature 0081, issue #184)**: repositories live in the `grid_db.repositories` **package**, not a flat module — `base.py` (`BaseRepository` + `T`), `identity.py` (User/BybitAccount/ApiCredential/Strategy/Run), `market_data.py` (PublicTrade/TickerSnapshot), `execution.py` (PrivateExecution/Order), `snapshots.py` (Position/Wallet/GridState). Add a new repository to the matching domain module **and re-export it from `repositories/__init__.py`**. Both `from grid_db import XRepository` and `from grid_db.repositories import XRepository` must keep resolving (guarded by `shared/db/tests/test_repository_imports.py`).

### Enums

- `RunType`: `RunType.LIVE`, `RunType.BACKTEST`, `RunType.SHADOW` — StrEnum in `grid_db.enums`

### Environment Variables

`GRIDBOT_DB_TYPE`, `GRIDBOT_DB_NAME`, `GRIDBOT_DB_HOST`, `GRIDBOT_DB_PORT`, `GRIDBOT_DB_USER`, `GRIDBOT_DB_PASSWORD`

---

