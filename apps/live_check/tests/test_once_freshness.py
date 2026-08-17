"""0100: --once freshness gate (previously watch-only).

A stopped recorder leaves a self-consistent data prefix; before 0100 the
--once path had no staleness probe, so such a prefix could PASS. These tests
pin the gate: stale ticker → SKIP before any replay is attempted; fresh
ticker → the per-strat check runs.
"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from grid_db import TickerSnapshot

from live_check import main as lc_main


def _args():
    return SimpleNamespace(last="1h", lag="2m", per_fill=False, curve=False)


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
