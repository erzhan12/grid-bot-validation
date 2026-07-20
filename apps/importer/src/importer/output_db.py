"""Output DB lifecycle for the importer (feature 0093).

Open/create the per-symbol output DB (recorder schema + WAL), maintain the
synthetic parent chain and the single ``recording`` run row, provide the
per-batch-commit insert wrapper and resume cursor reads, and manage the
``.importlock`` sidecar (O_EXCL, PID + start time, stale-PID reclaim).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import func

from grid_db.database import DatabaseFactory
from grid_db.identity import account_id_for, strategy_id_for, user_id_for
from grid_db.models import BybitAccount, Run, Strategy, TickerSnapshot, User
from grid_db.repositories.identity import RunRepository
from grid_db.repositories.market_data import TickerSnapshotRepository
from grid_db.settings import DatabaseSettings

logger = logging.getLogger(__name__)

# Pinned deterministic identity (recorder-style uuid5): reruns session.get
# the same parent rows instead of inserting duplicates. The run row's
# account_id must be non-null — replay's snapshot writer raises on NULL.
_IMPORTER_NAME = "importer"
IMPORTER_USER_ID = user_id_for(_IMPORTER_NAME)
IMPORTER_ACCOUNT_ID = account_id_for(_IMPORTER_NAME)


class ImportLockHeldError(Exception):
    """The ``.importlock`` sidecar is held (or unreadable) — do not write."""


def output_db_path(out_dir: str, symbol: str, tag: Optional[str] = None) -> Path:
    """``{out_dir}/imported_<symbol>[_<tag>].db`` — stable default path.

    The filename deliberately does NOT embed start/end: the default
    ``--end`` resolves to a live MAX(timestamp) probe, so a range-encoded
    name would defeat incremental resume.
    """
    suffix = f"_{tag}" if tag else ""
    return Path(out_dir) / f"imported_{symbol}{suffix}.db"


def lock_path(db_path: Path) -> Path:
    """Sidecar lock path next to the output DB."""
    return db_path.with_name(db_path.name + ".importlock")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — treat as alive.
        return True
    return True


def _reclaim_or_abort(path: Path) -> None:
    """Reclaim a stale (dead-PID) lock with a WARNING, else abort."""
    try:
        content = path.read_text()
        pid = int(
            next(
                line.split("=", 1)[1]
                for line in content.splitlines()
                if line.startswith("pid=")
            )
        )
    except (OSError, StopIteration, ValueError) as e:
        raise ImportLockHeldError(
            f"unreadable import lock {path}; inspect and rm it manually"
        ) from e
    if _pid_alive(pid):
        # PID-reuse edge (recycled pid of a dead importer) is accepted:
        # recovery is a documented manual rm; the stored start time aids
        # diagnosis but is not used for automatic reclaim.
        raise ImportLockHeldError(
            f"import lock {path} held by live pid {pid}; wait for it or, "
            "if the pid was recycled, rm the lock manually"
        )
    logger.warning("stale import lock %s (dead pid %d) — reclaiming", path, pid)
    path.unlink()


def acquire_lock(db_path: Path) -> Path:
    """Acquire the O_EXCL ``.importlock`` sidecar; returns the lock path.

    Excludes concurrent importers only — it cannot detect an in-flight
    replay, which takes no lock (sweep separation is an operational rule).
    """
    path = lock_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        _reclaim_or_abort(path)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            # A concurrent importer won the post-reclaim race.
            raise ImportLockHeldError(
                f"import lock {path} was re-acquired by a concurrent importer"
            ) from e
    with os.fdopen(fd, "w") as f:
        f.write(
            f"pid={os.getpid()}\n"
            f"start={datetime.now(timezone.utc).isoformat()}\n"
        )
    return path


def release_lock(path: Path) -> None:
    """Remove the sidecar lock (idempotent)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def open_output_db(db_path: Path) -> DatabaseFactory:
    """Create/open the output DB with full recorder schema and WAL."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = DatabaseFactory(DatabaseSettings(database_url=f"sqlite:///{db_path}"))
    db.create_tables()
    # DatabaseFactory's connect listener sets only PRAGMA foreign_keys —
    # WAL must be issued explicitly (persistent per file; idempotent).
    with db.engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    return db


def ensure_parents(db: DatabaseFactory, symbol: str) -> str:
    """Insert-if-missing synthetic User/BybitAccount/Strategy; returns strategy_id."""
    strategy_id = strategy_id_for(f"{_IMPORTER_NAME}_{symbol.lower()}")
    with db.get_session() as session:
        if session.get(User, IMPORTER_USER_ID) is None:
            session.add(User(user_id=IMPORTER_USER_ID, username=_IMPORTER_NAME))
        if session.get(BybitAccount, IMPORTER_ACCOUNT_ID) is None:
            session.add(
                BybitAccount(
                    account_id=IMPORTER_ACCOUNT_ID,
                    user_id=IMPORTER_USER_ID,
                    account_name=_IMPORTER_NAME,
                    environment="mainnet",
                )
            )
        if session.get(Strategy, strategy_id) is None:
            session.add(
                Strategy(
                    strategy_id=strategy_id,
                    account_id=IMPORTER_ACCOUNT_ID,
                    strategy_type="GridStrategy",
                    symbol=symbol,
                    config_json={"note": "synthetic importer stub"},
                )
            )
    return strategy_id


def get_resume_ts(db: DatabaseFactory, symbol: str) -> Optional[datetime]:
    """Resume cursor: MAX(exchange_ts) already in the output DB."""
    with db.get_session() as session:
        return TickerSnapshotRepository(session).get_last_ticker_ts(symbol)


def get_min_ts(db: DatabaseFactory, symbol: str) -> Optional[datetime]:
    """MIN(exchange_ts) in the output DB (prefix-guard input)."""
    with db.get_session() as session:
        return (
            session.query(func.min(TickerSnapshot.exchange_ts))
            .filter(TickerSnapshot.symbol == symbol)
            .scalar()
        )


def insert_batch(db: DatabaseFactory, snapshots: List[TickerSnapshot]) -> int:
    """Insert one batch inside its own session — one commit per batch.

    ``bulk_insert`` only flushes; the commit happens at ``get_session()``
    exit. A single long session would lose ALL batches on a crash and make
    the ``get_last_ticker_ts`` resume cursor a lie; per-batch commit bounds
    crash loss to one uncommitted batch.
    """
    with db.get_session() as session:
        return TickerSnapshotRepository(session).bulk_insert(snapshots)


def ensure_run_row(
    db: DatabaseFactory, symbol: str, source_desc: str
) -> Optional[str]:
    """Create-or-update the single synthetic ``recording`` run row.

    ``start_ts``/``end_ts`` always come from the DB-wide
    MIN/MAX(exchange_ts) — never the session's own min/max, which on
    append would silently shrink the auto-discovered replay window. Runs
    unconditionally when the DB holds >= 1 ticker row, healing a run row
    left truncated by a crash between a committed batch and this step.

    Returns the run_id, or None under the zero-row rule (no ticker rows:
    ``Run.start_ts`` is nullable=False and replay aborts on an invalid
    range, so no run row is created).
    """
    with db.get_session() as session:
        min_ts, max_ts = (
            session.query(
                func.min(TickerSnapshot.exchange_ts),
                func.max(TickerSnapshot.exchange_ts),
            )
            .filter(TickerSnapshot.symbol == symbol)
            .one()
        )
        if min_ts is None:
            return None
        # Never insert a second recording row: get_latest_by_type orders by
        # start_ts DESC only, so two rows with equal start_ts would make
        # replay auto-discovery nondeterministic.
        run = RunRepository(session).get_latest_by_type("recording")
        if run is None:
            run = Run(
                run_id=str(uuid4()),
                user_id=IMPORTER_USER_ID,
                account_id=IMPORTER_ACCOUNT_ID,
                strategy_id=strategy_id_for(f"{_IMPORTER_NAME}_{symbol.lower()}"),
                run_type="recording",
                config_snapshot={
                    "note": "imported from trad_save_history",
                    "symbol": symbol,
                    "source": source_desc,
                },
                start_ts=min_ts,
                end_ts=max_ts,
                status="completed",
            )
            session.add(run)
        else:
            run.start_ts = min_ts
            run.end_ts = max_ts
        return run.run_id
