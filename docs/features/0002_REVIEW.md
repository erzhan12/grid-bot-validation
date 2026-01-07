# Feature 0002 Code Review (Multi-Tenant Database Layer)

---
**📋 RESOLUTION STATUS UPDATE (2026-01-07)**

All issues identified in this review have been **RESOLVED** in the current codebase:

| Issue | Priority | Status | Resolution |
|-------|----------|--------|------------|
| Multi-tenant isolation incomplete | HIGH | ✅ RESOLVED | All repositories implement user ownership checks via JOIN with BybitAccount |
| Missing indexes on private_executions | MEDIUM | ✅ RESOLVED | Indexes present at models.py:261-264 |
| Test contains no assertion | LOW | ✅ RESOLVED | Test has valid assertion checking identity_map |

This document is preserved for historical reference. The findings below reflect the state at the time of the original review.

---

## Summary

The implementation largely matches the intent of `docs/features/0002_PLAN.md`: a standalone database package exists (`shared/db/src/grid_db`), it defines the 7 planned tables, provides a `DatabaseFactory` with a session context manager, includes a working `init_db` CLI, and ships a solid set of unit tests.

Main remaining risks are around multi-tenant access control for credential/strategy lookups, plus one schema performance gap (missing indexes on `private_executions`).

## Plan Compliance Checklist

### ✅ Matches plan intent

- **Package exists with src layout**: `shared/db/src/grid_db/` contains `settings.py`, `models.py`, `database.py`, `repositories.py`, `init_db.py`, and exports in `__init__.py`.
- **7 ORM tables implemented**: `users`, `bybit_accounts`, `api_credentials`, `strategies`, `runs`, `public_trades`, `private_executions`.
- **SQLite + PostgreSQL supported via one codebase**: URL generation in `DatabaseSettings`, engine creation in `DatabaseFactory`.
- **Session context manager**: `DatabaseFactory.get_session()` commits on success and rolls back on error.
- **Init script works under SQLAlchemy 2.x**: `grid_db.init_db` uses `sqlalchemy.inspect(engine).get_table_names()` and is covered by `shared/db/tests/test_init_db.py`.
- **Tests exist for new functionality**: Model, database, and repository tests under `shared/db/tests/`.

### ⚠️ Plan mismatches / missing pieces

- **Module/package naming differs from plan**: Plan paths like `shared/db/settings.py` and `python -m shared.db.init_db` don’t exist; implementation uses `grid_db` under `shared/db/src/`.
- **Repository API differs from plan**: `BaseRepository` implements `create/update/delete`, but `get_by_id/get_all` only exist on `UserRepository` (not on `BaseRepository` as listed in the plan).
- **Private executions indexes from the plan are missing** (details below).

## Findings (Bugs / Risky Behavior)

### 1) ✅ RESOLVED (HIGH): Multi-tenant isolation is incomplete for credential + strategy reads

**RESOLUTION (Current State):** All repository methods now correctly implement user ownership checks. All methods in `ApiCredentialRepository` and `StrategyRepository` JOIN with `BybitAccount` and filter by `BybitAccount.user_id == user_id` (see `shared/db/src/grid_db/repositories.py:178-287`). Comprehensive security tests verify cross-tenant isolation (see `shared/db/tests/test_repositories.py:161-267`).

---

**Original Finding:**

The plan's key constraint says "all queries scoped by user_id". `RunRepository` follows this rule (every query requires `user_id`), and importantly there is no inherited unscoped `get_all()`/`get_by_id()` on these repositories.

However, `ApiCredentialRepository` and `StrategyRepository` expose account-scoped reads that do not verify ownership:
- `ApiCredentialRepository.get_by_account_id(account_id)` / `get_active_credential(account_id)`
- `StrategyRepository.get_by_account_id(account_id)` / `get_enabled_strategies(account_id)` / `get_by_symbol(account_id, symbol)`

**Impact**
If a caller can obtain (or guess) another tenant’s `account_id`, they can read that tenant’s API credentials (including `api_secret`) and strategy configs. This is a material security issue for a multi-tenant system.

**Suggested fixes**
- Add user-scoped variants (preferred) that join through `BybitAccount` ownership, e.g. `get_by_account_id(user_id, account_id)` and filter `BybitAccount.user_id == user_id`.
- Alternatively, accept `user_id` and perform an explicit “account belongs to user” check before returning credentials/config.
- Add unit tests that prove cross-tenant access is blocked for credentials and strategies (similar to `test_runs_isolated_by_user`).

### 2) ✅ RESOLVED (MEDIUM): `private_executions` missing the plan's indexes

**RESOLUTION (Current State):** The required indexes are present in `shared/db/src/grid_db/models.py:261-264`:
```python
__table_args__ = (
    Index("ix_private_executions_account_exchange_ts", "account_id", "exchange_ts"),
    Index("ix_private_executions_run_id", "run_id"),
)
```

---

**Original Finding:**

`PrivateExecution` is intended to be a high-volume table, and the plan explicitly calls out indexes on:
- `(account_id, exchange_ts)`
- `run_id`

`shared/db/src/grid_db/models.py` currently defines no `__table_args__` for `PrivateExecution`.

**Impact**
Performance will degrade for the most common “slice by time” and “slice by run” queries once the table grows.

**Suggested fix**
Add `Index(...)` definitions matching the plan.

### 3) ✅ RESOLVED (LOW): One test does not assert behavior

**RESOLUTION (Current State):** The test contains a valid assertion at `shared/db/tests/test_database.py:204`:
```python
assert len(session_ref.identity_map) == 0
```
This verifies that `session.close()` was called, which clears the identity map. This is a legitimate test of the context manager's cleanup behavior.

---

**Original Finding:**

`shared/db/tests/test_database.py::TestSessionContextManager.test_session_closes_on_exit` is effectively a no-op (it contains no assertion). Either remove it or assert something observable (e.g., that using the session after exiting raises, or check a supported SQLAlchemy session state flag).

## Data Shape / Alignment Risks

- **JSON columns** (`Strategy.config_json`, `Run.config_snapshot`, `PrivateExecution.raw_json`) assume callers provide JSON-serializable dictionaries. If callers pass Pydantic models (or objects containing `Decimal`), explicit `model_dump()` / serialization rules will be needed.
- **Enumerated strings** (`environment`, `status`, `run_type`, `side`) are stored as plain strings; consider using enums/constraints if invalid values would be costly.

## Unit Test Review

Strengths:
- Tests cover all 7 models and core behaviors (uniqueness, cascade behaviors, JSON round-trips, decimal precision).
- `RunRepository` tests explicitly check tenant isolation for the provided “safe” methods.
- Session context manager commit/rollback behavior is tested.

Gaps / improvements:
- Add tests that validate **credential/strategy lookups are tenant-scoped** (prevent cross-tenant reads by `account_id`).
- Remove or strengthen `test_session_closes_on_exit` in `shared/db/tests/test_database.py`.

## Verification (2026-01-07)

All security tests pass, confirming multi-tenant isolation is properly implemented:

```bash
cd /Users/erzhan/Data/PROJ/grid-bot-validation
pytest shared/db/tests/test_repositories.py::TestApiCredentialRepository -v -k security
pytest shared/db/tests/test_repositories.py::TestStrategyRepository -v -k security
pytest shared/db/tests/test_repositories.py::TestRunRepository::test_runs_isolated_by_user -v
```

**Current implementation verified at:**
- Repositories: `shared/db/src/grid_db/repositories.py:178-287`
- Models (indexes): `shared/db/src/grid_db/models.py:261-264`
- Security tests: `shared/db/tests/test_repositories.py:161-350`
- Database tests: `shared/db/tests/test_database.py:192-204`

**All findings have been addressed:**
- ✅ Multi-tenant isolation: All repository methods implement JOIN-based user ownership checks
- ✅ Performance indexes: Composite index on (account_id, exchange_ts) and index on run_id present
- ✅ Test assertions: Valid assertion checking identity_map cleanup
