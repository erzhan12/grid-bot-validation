"""Replay orchestration for live_check — one seeded event_follower run per strat."""

import logging

from grid_db import DatabaseFactory

from replay.config import FillSimulatorConfig, ReplayConfig, SeedConfig
from replay.engine import ReplayEngine, ReplayResult

from live_check.config import StratCheckConfig
from live_check.window import Window

logger = logging.getLogger(__name__)


def build_replay_config(
    strat: StratCheckConfig,
    window: Window,
    run_id: str,
    database_url: str,
    account_id: str,
) -> ReplayConfig:
    """Compose a per-strat ReplayConfig for a seeded event_follower run.

    ``account_id`` MUST be passed in (pre-queried from the ``Run`` row by the
    caller): ``SeedConfig.require_seed_fields_when_enabled`` rejects
    ``enabled=True`` without it at CONSTRUCTION time — the engine's own
    ``Run.account_id`` resolution happens too late.

    Args:
        strat: Strat geometry+risk mirror.
        window: Rolling comparison window (naive-UTC).
        run_id: Recorder run identifier.
        database_url: Live recorder DB URL (opened read-only by the caller).
        account_id: ``Run.account_id`` of the recorder run.

    Returns:
        ReplayConfig ready for ``ReplayEngine``.
    """
    return ReplayConfig(
        database_url=database_url,
        run_id=run_id,
        symbol=strat.symbol,
        start_ts=window.start,
        end_ts=window.end,
        strategy=strat.to_replay_strategy_config(),
        seed=SeedConfig(
            enabled=True,
            at_ts=window.start,
            account_id=account_id,
            strat_id=strat.strat_id,
        ),
        fill_simulator=FillSimulatorConfig(mode="event_follower"),
    )


def run_strat(
    strat: StratCheckConfig,
    window: Window,
    run_id: str,
    account_id: str,
    db: DatabaseFactory,
) -> ReplayResult:
    """Run one seeded event_follower replay for a strat over the window.

    ``db`` is the READ-ONLY live recorder factory; snapshot emission is
    disabled so the engine never writes ``source='backtest'`` rows into it
    (Phase 1B(b)).
    """
    config = build_replay_config(
        strat=strat,
        window=window,
        run_id=run_id,
        database_url=db.settings.get_database_url(),
        account_id=account_id,
    )
    engine = ReplayEngine(config, db=db, emit_backtest_snapshots=False)
    logger.info(
        "%s: replaying %s window %s → %s (event_follower, seeded)",
        strat.strat_id, strat.symbol, window.start, window.end,
    )
    return engine.run()
