"""Tests for --watch tick resilience (skip-on-seed-miss, no crash)."""

from datetime import datetime, timedelta
from decimal import Decimal

from grid_db import PrivateExecution, TickerSnapshot
from replay.snapshot_loader import SeedDataQualityError

from live_check import main as lc_main
from live_check.window import staleness_threshold

_NOW = datetime(2026, 7, 1, 12, 0, 0)
_LAG = timedelta(minutes=2)


def _seed_window_data(db, ts):
    """One exec + one fresh ticker so the tick reaches the seed path."""
    with db.get_session() as session:
        session.add(PrivateExecution(
            run_id="test-run-id",
            account_id="acc1",
            symbol="LTCUSDT",
            exec_id="e1",
            order_id="o1",
            order_link_id="L1",
            exchange_ts=ts - timedelta(minutes=30),
            side="Buy",
            exec_price=Decimal("80"),
            exec_qty=Decimal("0.2"),
            exec_fee=Decimal("0.01"),
            closed_pnl=Decimal("0"),
        ))
        session.add(TickerSnapshot(
            symbol="LTCUSDT",
            exchange_ts=ts - timedelta(minutes=3),
            local_ts=ts - timedelta(minutes=3),
            last_price=Decimal("80"),
            mark_price=Decimal("80"),
            bid1_price=Decimal("79.9"),
            ask1_price=Decimal("80.1"),
            funding_rate=Decimal("0.0001"),
        ))


class TestWatchSeedMiss:
    def test_seed_miss_renders_skip_and_loop_continues(
        self, db, seeded_run_account, strat, live_check_config, monkeypatch
    ):
        """SeedDataQualityError at window.start → SKIP line, no crash.

        Two consecutive ticks both complete — proves the watch loop survives
        a per-tick seed miss instead of dying on the exception.
        """
        _seed_window_data(db, _NOW)

        def _raise_seed_miss(*args, **kwargs):
            raise SeedDataQualityError("no grid state at window start")

        monkeypatch.setattr(lc_main.runner, "run_strat", _raise_seed_miss)

        threshold = staleness_threshold(_LAG)
        for _ in range(2):  # loop continues across ticks
            lines = lc_main.watch_tick(
                live_check_config, db, seeded_run_account.run_id,
                seeded_run_account.account_id, timedelta(hours=1), _LAG,
                threshold, now=_NOW,
            )
            assert len(lines) == 1
            assert "SKIP" in lines[0]
            assert "seed miss" in lines[0]

    def test_stale_data_renders_skip_line(
        self, db, seeded_run_account, strat, live_check_config
    ):
        """Frozen recorder (stale ticker) → SKIP line, replay never invoked."""
        with db.get_session() as session:
            session.add(TickerSnapshot(
                symbol="LTCUSDT",
                exchange_ts=_NOW - timedelta(hours=3),
                local_ts=_NOW - timedelta(hours=3),
                last_price=Decimal("80"),
                mark_price=Decimal("80"),
                bid1_price=Decimal("79.9"),
                ask1_price=Decimal("80.1"),
                funding_rate=Decimal("0.0001"),
            ))
        threshold = staleness_threshold(_LAG)
        lines = lc_main.watch_tick(
            live_check_config, db, seeded_run_account.run_id,
            seeded_run_account.account_id, timedelta(hours=1), _LAG,
            threshold, now=_NOW,
        )
        assert len(lines) == 1
        assert "SKIP" in lines[0]
        assert "stale" in lines[0]


class TestWatchWindowOverride:
    def test_cli_last_reaches_reconcile_window(
        self, db, seeded_run_account, strat, live_check_config, monkeypatch
    ):
        """The `last` passed into watch_tick sizes the window — a --last
        override must NOT be silently replaced by config.last (4h default)."""
        _seed_window_data(db, _NOW)
        captured = {}

        def _capture(strat_, window, *args, **kwargs):
            captured["window"] = window
            return ("skip", "captured")

        monkeypatch.setattr(lc_main, "check_strat", _capture)
        threshold = staleness_threshold(_LAG)
        lc_main.watch_tick(
            live_check_config, db, seeded_run_account.run_id,
            seeded_run_account.account_id, timedelta(minutes=30), _LAG,
            threshold, now=_NOW,
        )
        window = captured["window"]
        assert window.end - window.start == timedelta(minutes=30)


class TestExitCodes:
    def test_fail_beats_skip_beats_pass(self):
        """Exit priority: any FAIL → 1; else any SKIP → 2; else 0."""
        assert lc_main._exit_code(["pass", "pass"]) == lc_main.EXIT_PASS
        assert lc_main._exit_code(["pass", "skip"]) == lc_main.EXIT_SKIP
        assert lc_main._exit_code(["skip", "fail"]) == lc_main.EXIT_FAIL
        assert lc_main._exit_code(["skip"]) == lc_main.EXIT_SKIP
