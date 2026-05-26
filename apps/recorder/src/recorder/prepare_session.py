"""Prepare the shared SQLite DB for a recorder session.

Three responsibilities, one entry point:

1. Surgical wipe (Phase 5 §5.1 + §5.2): clear recorder-owned rows and any
   recording runs from a prior session. Always runs.
2. Identity bootstrap (Step 3): insert-if-missing the gridbot-style
   User / BybitAccount / Strategy rows so the recorder's verify-only
   `_seed_db_records` succeeds on a clean DB without requiring gridbot to
   start first. Only when `account:` is configured.
3. Preflight verify (Step 4): run the same `verify_shared_db_parents`
   contract the recorder uses, so stale preserved rows fail BEFORE the
   recorder is launched into a guaranteed `_seed_db_records` failure.
   Only when `account:` is configured.

All three steps share the same `load_config`-resolved database URL, so
shell-level YAML parsing in `start_recorder.sh` is removed.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import make_url

from grid_db import (
    BybitAccount,
    DatabaseFactory,
    DatabaseSettings,
    Strategy,
    User,
)
from grid_db.identity import account_id_for, strategy_id_for, user_id_for
from gridbot.config import GridbotConfig, load_config as load_gridbot_config

from recorder.config import RecorderConfig, load_config as load_recorder_config
from recorder.shared_db_parents import (
    SharedDbParentError,
    verify_shared_db_parents,
)


def _sqlite_filesystem_path(database_url: str) -> Optional[str]:
    """Resolve a SQLite URL to a filesystem path (None if `:memory:`).

    Accepts both `sqlite:///...` and `sqlite+pysqlite:///...` forms. Raises
    on non-SQLite dialects — Phase 4 prepare scripts are SQLite-only.
    """
    url = make_url(database_url)
    if url.drivername not in ("sqlite", "sqlite+pysqlite"):
        raise ValueError(
            f"Phase 4 prepare scripts require SQLite; got {url.drivername!r}"
        )
    if url.database in (None, "", ":memory:"):
        return None
    return url.database


def _wipe_recorder_data(database_url: str) -> None:
    """§5.1 + §5.2 surgical wipe in one transaction.

    No-op if the DB is `:memory:` or the file doesn't yet exist (clean
    install). Runs whether or not `account:` is configured — both modes
    need the 0049 per-restart session reset.
    """
    fs_path = _sqlite_filesystem_path(database_url)
    if fs_path is None:
        print("prepare_session: in-memory DB; wipe is a no-op", flush=True)
        return

    import os

    if not os.path.isfile(fs_path):
        print(
            f"prepare_session: no DB at {fs_path}; recorder will create it",
            flush=True,
        )
        return

    settings = DatabaseSettings(database_url=database_url)
    db = DatabaseFactory(settings)
    # Ensure schema exists before DELETE. Handles the case where the file
    # exists but is empty/schemaless (operator touched the file, mounted
    # empty volume, or interrupted prior setup) — without this, the wipe
    # DELETEs would raise `no such table: private_executions` instead of
    # being a no-op on a fresh DB. create_tables() is idempotent.
    db.create_tables()

    print(f"prepare_session: wiped {fs_path} (§5.1+§5.2)", flush=True)
    print("  §5.1: delete placeholder account_id rows (migration)", flush=True)
    print(
        "  §5.2: delete all recorder-owned rows + recording runs "
        "(0049 session reset)",
        flush=True,
    )
    print(
        "  preserving: grid_state_snapshots, live runs, bybit_accounts, "
        "strategies, users",
        flush=True,
    )

    placeholder_account_id = "00000000-0000-0000-0000-000000000002"

    with db.engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON"))
        # §5.1 — one-time placeholder cleanup (idempotent after migration).
        conn.execute(
            text("DELETE FROM private_executions WHERE account_id = :pid"),
            {"pid": placeholder_account_id},
        )
        conn.execute(
            text("DELETE FROM orders WHERE account_id = :pid"),
            {"pid": placeholder_account_id},
        )
        conn.execute(
            text("DELETE FROM wallet_snapshots WHERE account_id = :pid"),
            {"pid": placeholder_account_id},
        )
        conn.execute(
            text(
                "DELETE FROM position_snapshots "
                "WHERE source = 'live' AND account_id = :pid"
            ),
            {"pid": placeholder_account_id},
        )
        conn.execute(
            text(
                "DELETE FROM runs "
                "WHERE run_type = 'recording' AND account_id = :pid"
            ),
            {"pid": placeholder_account_id},
        )
        # §5.2 — broad recording-run wipe (every restart).
        conn.execute(text("DELETE FROM private_executions"))
        conn.execute(text("DELETE FROM orders"))
        conn.execute(text("DELETE FROM wallet_snapshots"))
        conn.execute(text("DELETE FROM position_snapshots WHERE source = 'live'"))
        conn.execute(text("DELETE FROM runs WHERE run_type = 'recording'"))


def _bootstrap_and_verify_parents(
    db: DatabaseFactory,
    config: RecorderConfig,
    gridbot_config: GridbotConfig,
) -> None:
    """Insert-if-missing parent rows, then verify gridbot-compatible metadata."""
    if not config.symbols:
        print(
            "ERROR: No symbols configured. Add symbols to recorder.yaml",
            file=sys.stderr,
        )
        raise SystemExit(1)

    assert config.account is not None  # caller-guarded

    name = config.account.name
    strat_id = config.account.strat_id
    primary_symbol = config.symbols[0]

    account_config = gridbot_config.get_account(name)
    if account_config is None:
        print(
            f"ERROR: account {name!r} not found in gridbot config",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if config.testnet != account_config.testnet:
        print(
            f"ERROR: recorder testnet={config.testnet} does not match gridbot "
            f"accounts[].testnet for {name!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    candidates = [
        s
        for s in gridbot_config.get_strategies_for_account(name)
        if s.strat_id == strat_id
    ]
    if len(candidates) == 0:
        print(
            f"ERROR: strat_id {strat_id!r} not found in gridbot config for "
            f"account {name!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if len(candidates) > 1:
        print(
            f"ERROR: ambiguous strat_id {strat_id!r} for account {name!r} — "
            f"{len(candidates)} matches in gridbot config",
            file=sys.stderr,
        )
        raise SystemExit(1)
    strat_config = candidates[0]

    if strat_config.symbol != primary_symbol:
        print(
            f"ERROR: gridbot Strategy symbol {strat_config.symbol!r} does not "
            f"match recorder primary symbol {primary_symbol!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    user_id = user_id_for(name)
    account_id = account_id_for(name)
    strategy_id = strategy_id_for(strat_id)
    environment = "testnet" if account_config.testnet else "mainnet"

    db.create_tables()

    created_any = False
    with db.get_session() as session:
        if session.get(User, user_id) is None:
            session.add(User(user_id=user_id, username=name))
            created_any = True
        if session.get(BybitAccount, account_id) is None:
            session.add(
                BybitAccount(
                    account_id=account_id,
                    user_id=user_id,
                    account_name=name,
                    environment=environment,
                )
            )
            created_any = True
        if session.get(Strategy, strategy_id) is None:
            session.add(
                Strategy(
                    strategy_id=strategy_id,
                    account_id=account_id,
                    strategy_type="GridStrategy",
                    symbol=strat_config.symbol,
                    config_json=strat_config.model_dump(mode="json"),
                )
            )
            created_any = True

    with db.get_session() as session:
        verify_shared_db_parents(
            session,
            user_id=user_id,
            account_id=account_id,
            strategy_id=strategy_id,
            account_name=name,
            strat_id=strat_id,
            primary_symbol=primary_symbol,
            recorder_testnet=config.testnet,
        )

    if created_any:
        print(
            "prepare_session: created User/BybitAccount/Strategy; "
            "parents verified",
            flush=True,
        )
    else:
        print(
            "prepare_session: parents already present; verified",
            flush=True,
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Surgical wipe + identity bootstrap + preflight verify on the "
            "shared recorder DB. Must succeed before start_recorder.sh "
            "launches the recorder."
        )
    )
    parser.add_argument(
        "recorder_config",
        help="Path to recorder YAML config.",
    )
    parser.add_argument(
        "--gridbot-config",
        default="conf/gridbot_test.yaml",
        help="Path to gridbot YAML config (default: conf/gridbot_test.yaml).",
    )
    args = parser.parse_args(argv)

    try:
        config = load_recorder_config(args.recorder_config)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: failed to load recorder config: {e}", file=sys.stderr)
        return 1

    try:
        _wipe_recorder_data(config.database_url)
    except Exception as e:
        print(f"ERROR: wipe failed: {e}", file=sys.stderr)
        return 1

    if config.account is None:
        return 0

    try:
        gridbot_config = load_gridbot_config(args.gridbot_config)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: failed to load gridbot config: {e}", file=sys.stderr)
        return 1

    settings = DatabaseSettings(database_url=config.database_url)
    db = DatabaseFactory(settings)

    try:
        _bootstrap_and_verify_parents(db, config, gridbot_config)
    except SharedDbParentError as e:
        print(f"ERROR: shared-DB parent verify failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
