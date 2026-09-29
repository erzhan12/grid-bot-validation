"""0100: --once freshness gate (previously watch-only).

A stopped recorder leaves a self-consistent data prefix; before 0100 the
--once path had no staleness probe, so such a prefix could PASS. These tests
pin the gate: stale ticker → SKIP before any replay is attempted; fresh
ticker → the per-strat check runs.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from grid_db import PrivateExecution, RecordedDataQualityError, TickerSnapshot

from live_check import main as lc_main


def _args():
    return SimpleNamespace(last="1h", lag="2m", per_fill=False, curve=False)


def _add_exec(db, exchange_ts, closed_pnl):
    with db.get_session() as session:
        session.add(PrivateExecution(
            run_id="test-run-id",
            account_id="acc1",
            symbol="LTCUSDT",
            exec_id="e1",
            order_id="o1",
            order_link_id="L1",
            exchange_ts=exchange_ts,
            side="Sell",
            exec_price=Decimal("80"),
            exec_qty=Decimal("0.2"),
            exec_fee=Decimal("0.01"),
            closed_pnl=closed_pnl,
        ))


def _add_ticker(db, exchange_ts):
    with db.get_session() as session:
        session.add(TickerSnapshot(
            symbol="LTCUSDT",
            exchange_ts=exchange_ts,
            local_ts=exchange_ts,
            last_price=Decimal("80"),
            mark_price=Decimal("80"),
            bid1_price=Decimal("79.9"),
            ask1_price=Decimal("80.1"),
            funding_rate=Decimal("0.0001"),
        ))


class TestOnceFreshnessGate:
    def test_stale_ticker_skips_before_replay(
        self, db, seeded_run_account, live_check_config, monkeypatch, capsys
    ):
        """Ticker frozen in the past → EXIT_SKIP, replay never invoked."""
        _add_ticker(db, datetime(2026, 7, 1, 9, 0, 0))  # ancient vs real now

        def _boom(*args, **kwargs):
            raise AssertionError("replay must not run on stale data")

        monkeypatch.setattr(lc_main, "check_strat", _boom)
        rc = lc_main.run_single(live_check_config, _args(), db)
        assert rc == lc_main.EXIT_SKIP
        out = capsys.readouterr().out
        assert "SKIP" in out
        assert "stale" in out

    def test_no_ticker_rows_skips_before_replay(
        self, db, seeded_run_account, live_check_config, monkeypatch, capsys
    ):
        """Empty ticker table → 'no ticker data' SKIP from the gate itself."""
        def _boom(*args, **kwargs):
            raise AssertionError("replay must not run without ticker data")

        monkeypatch.setattr(lc_main, "check_strat", _boom)
        rc = lc_main.run_single(live_check_config, _args(), db)
        assert rc == lc_main.EXIT_SKIP
        assert "no ticker data" in capsys.readouterr().out

    def test_unknown_execution_pnl_skips_validation(
        self, db, seeded_run_account, live_check_config, monkeypatch, capsys
    ):
        """Fresh ticker + NULL closed_pnl in window → SKIP, no replay."""
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_ticker(db, now_naive)
        _add_exec(db, now_naive - timedelta(minutes=30), closed_pnl=None)

        def _boom(*args, **kwargs):
            raise AssertionError("replay must not run on unknown PnL")

        monkeypatch.setattr(lc_main.runner, "run_strat", _boom)
        rc = lc_main.run_single(live_check_config, _args(), db)
        assert rc == lc_main.EXIT_SKIP
        out = capsys.readouterr().out
        assert "LTCUSDT" in out and "SKIP" in out
        assert "unknown closed_pnl" in out

    def test_shared_unknown_execution_pnl_skips_validation(
        self, db, seeded_run_account, live_check_config, monkeypatch, capsys
    ):
        """--shared: NULL closed_pnl in window → SKIP before shared replay."""
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_ticker(db, now_naive)
        _add_exec(db, now_naive - timedelta(minutes=30), closed_pnl=None)

        def _boom(*args, **kwargs):
            raise AssertionError("shared replay must not run on unknown PnL")

        monkeypatch.setattr(lc_main.runner, "run_shared", _boom)
        rc = lc_main.run_shared_single(live_check_config, _args(), db)
        assert rc == lc_main.EXIT_SKIP
        out = capsys.readouterr().out
        assert "LTCUSDT" in out and "SKIP" in out
        assert "unknown closed_pnl" in out

    def test_shared_late_unknown_pnl_during_replay_skips(
        self, db, seeded_run_account, live_check_config, monkeypatch, capsys
    ):
        """--shared: a NULL landing after the pre-check → SKIP, no crash."""
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_ticker(db, now_naive)
        _add_exec(db, now_naive - timedelta(minutes=30),
                  closed_pnl=Decimal("0"))

        def _raise_quality(*args, **kwargs):
            raise RecordedDataQualityError("recorded execution e9 unknown")

        monkeypatch.setattr(lc_main.runner, "run_shared", _raise_quality)
        rc = lc_main.run_shared_single(live_check_config, _args(), db)
        assert rc == lc_main.EXIT_SKIP
        assert "recorded data quality" in capsys.readouterr().out

    def test_fresh_ticker_reaches_per_strat_check(
        self, db, seeded_run_account, live_check_config, monkeypatch
    ):
        """Fresh ticker → gate passes and check_strat IS invoked."""
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        _add_ticker(db, now_naive)

        called = []

        def _sentinel(*args, **kwargs):
            called.append(1)
            return ("skip", "sentinel")

        monkeypatch.setattr(lc_main, "check_strat", _sentinel)
        rc = lc_main.run_single(live_check_config, _args(), db)
        assert called, "freshness gate must let a fresh window through"
        assert rc == lc_main.EXIT_SKIP  # sentinel outcome, not the gate
