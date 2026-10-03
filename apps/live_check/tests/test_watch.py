"""Tests for --watch tick resilience (skip-on-seed-miss, no crash)."""

from datetime import datetime, timedelta
from decimal import Decimal

from grid_db import (
    DatabaseFactory,
    DatabaseSettings,
    PositionSnapshot,
    PrivateExecution,
    PrivateStreamGap,
    PrivateStreamSession,
    RecordedDataQualityError,
    TickerSnapshot,
)
from replay.snapshot_loader import SeedDataQualityError

from live_check import main as lc_main
from live_check.window import staleness_threshold

_NOW = datetime(2026, 7, 1, 12, 0, 0)
_LAG = timedelta(minutes=2)


def _seed_window_data(db, ts, closed_pnl=Decimal("0"), account_id="acc1"):
    """One exec + one fresh ticker so the tick reaches the seed path."""
    with db.get_session() as session:
        session.add(PrivateExecution(
            run_id="test-run-id",
            account_id=account_id,
            symbol="LTCUSDT",
            exec_id="e1",
            order_id="o1",
            order_link_id="L1",
            exchange_ts=ts - timedelta(minutes=30),
            side="Buy",
            exec_price=Decimal("80"),
            exec_qty=Decimal("0.2"),
            exec_fee=Decimal("0.01"),
            closed_pnl=closed_pnl,
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
        self, db, seeded_run_account, private_coverage, fit_anchors, strat,
        live_check_config, monkeypatch,
    ):
        """SeedDataQualityError at window.start → SKIP line, no crash.

        Two consecutive ticks both complete — proves the watch loop survives
        a per-tick seed miss instead of dying on the exception.
        """
        fit_anchors(_NOW - timedelta(minutes=5))
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

    def test_unknown_execution_pnl_skips_validation(
        self, db, seeded_run_account, private_coverage, fit_anchors, strat,
        live_check_config, monkeypatch,
    ):
        """NULL closed_pnl in the window → SKIP line, replay never invoked."""
        fit_anchors(_NOW - timedelta(minutes=5))
        _seed_window_data(db, _NOW, closed_pnl=None)

        def _boom(*args, **kwargs):
            raise AssertionError("replay must not run on unknown PnL")

        monkeypatch.setattr(lc_main.runner, "run_strat", _boom)
        lines = lc_main.watch_tick(
            live_check_config, db, seeded_run_account.run_id,
            seeded_run_account.account_id, timedelta(hours=1), _LAG,
            staleness_threshold(_LAG), now=_NOW,
        )
        assert len(lines) == 1
        assert "SKIP" in lines[0]
        assert "unknown closed_pnl" in lines[0]
        assert "LTCUSDT" in lines[0] or "ltcusdt_test" in lines[0]

    def test_late_unknown_pnl_during_replay_renders_skip(
        self, db, seeded_run_account, private_coverage, fit_anchors, strat,
        live_check_config, monkeypatch,
    ):
        """A NULL landing after the pre-check → SKIP line, tick survives."""
        fit_anchors(_NOW - timedelta(minutes=5))
        _seed_window_data(db, _NOW)

        def _raise_quality(*args, **kwargs):
            raise RecordedDataQualityError("recorded execution e9 unknown")

        monkeypatch.setattr(lc_main.runner, "run_strat", _raise_quality)
        lines = lc_main.watch_tick(
            live_check_config, db, seeded_run_account.run_id,
            seeded_run_account.account_id, timedelta(hours=1), _LAG,
            staleness_threshold(_LAG), now=_NOW,
        )
        assert len(lines) == 1
        assert "SKIP" in lines[0]
        assert "recorded data quality" in lines[0]

    def test_unknown_pnl_in_ground_truth_after_replay_renders_skip(
        self, db, seeded_run_account, private_coverage, fit_anchors, strat,
        live_check_config, monkeypatch,
    ):
        """Replay succeeds, then collect() meets a NULL → SKIP, tick survives."""
        fit_anchors(_NOW - timedelta(minutes=5))
        _seed_window_data(db, _NOW)

        def _raise_quality(*args, **kwargs):
            raise RecordedDataQualityError("unknown closed_pnl on 1 execution(s)")

        monkeypatch.setattr(lc_main.runner, "run_strat", lambda *a, **k: object())
        monkeypatch.setattr(lc_main.ground_truth, "collect", _raise_quality)
        lines = lc_main.watch_tick(
            live_check_config, db, seeded_run_account.run_id,
            seeded_run_account.account_id, timedelta(hours=1), _LAG,
            staleness_threshold(_LAG), now=_NOW,
        )
        assert len(lines) == 1
        assert "SKIP" in lines[0]
        assert "recorded data quality" in lines[0]

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


class TestWatchPrivateCoverage:
    """0110 B2b: the coverage gate turns into SKIP lines, never a crash."""

    def test_no_coverage_renders_skip_line(
        self, db, seeded_run_account, strat, live_check_config, monkeypatch
    ):
        """No session row → SKIP line, replay never invoked."""
        _seed_window_data(db, _NOW)

        def _boom(*args, **kwargs):
            raise AssertionError("no verdict without private coverage")

        monkeypatch.setattr(lc_main.runner, "run_strat", _boom)
        lines = lc_main.watch_tick(
            live_check_config, db, seeded_run_account.run_id,
            seeded_run_account.account_id, timedelta(hours=1), _LAG,
            staleness_threshold(_LAG), now=_NOW,
        )
        assert len(lines) == 1
        assert "SKIP" in lines[0]
        assert "no private-stream session" in lines[0]

    def test_gap_renders_skip_line(
        self, db, seeded_run_account, private_coverage, strat,
        live_check_config, monkeypatch,
    ):
        """Covering session but an overlapping gap → gap SKIP line."""
        _seed_window_data(db, _NOW)
        with db.get_session() as session:
            session.add(PrivateStreamGap(
                run_id="test-run-id",
                account_id=seeded_run_account.account_id,
                symbol="LTCUSDT",
                gap_start=_NOW - timedelta(minutes=40),
                gap_end=None,
                recovery_status="pending",
                inserted=0,
                duplicates=0,
            ))

        def _boom(*args, **kwargs):
            raise AssertionError("no verdict across a private-stream gap")

        monkeypatch.setattr(lc_main.runner, "run_strat", _boom)
        lines = lc_main.watch_tick(
            live_check_config, db, seeded_run_account.run_id,
            seeded_run_account.account_id, timedelta(hours=1), _LAG,
            staleness_threshold(_LAG), now=_NOW,
        )
        assert len(lines) == 1
        assert "private-stream gap" in lines[0]

    def test_missing_coverage_tables_skip_without_killing_watch(
        self, tmp_path, live_check_config, monkeypatch
    ):
        """Pre-0110 DB (tables absent), opened mode=ro → SKIP line on every
        tick; nothing raises, so run_watch keeps looping."""
        url = f"sqlite:///{tmp_path}/recorder.db"
        writable = DatabaseFactory(DatabaseSettings(database_url=url))
        writable.create_tables()
        PrivateStreamGap.__table__.drop(writable.engine)
        PrivateStreamSession.__table__.drop(writable.engine)
        with writable.get_session() as session:  # fresh ticker: gate reached
            session.add(TickerSnapshot(
                symbol="LTCUSDT",
                exchange_ts=_NOW - timedelta(minutes=3),
                local_ts=_NOW - timedelta(minutes=3),
                last_price=Decimal("80"),
                mark_price=Decimal("80"),
                bid1_price=Decimal("79.9"),
                ask1_price=Decimal("80.1"),
                funding_rate=Decimal("0.0001"),
            ))
        ro = DatabaseFactory(DatabaseSettings(database_url=url, read_only=True))

        def _boom(*args, **kwargs):
            raise AssertionError("no verdict without private coverage")

        monkeypatch.setattr(lc_main.runner, "run_strat", _boom)
        for _ in range(2):
            lines = lc_main.watch_tick(
                live_check_config, ro, "test-run-id", "acc1",
                timedelta(hours=1), _LAG, staleness_threshold(_LAG), now=_NOW,
            )
            assert lines == [
                "ltcusdt_test SKIP: recorder has no private-stream coverage "
                "(pre-0110)"
            ]


class TestWatchEndAnchors:
    """0110 B2c: end-of-window position anchors gate the verdict."""

    def test_fresh_public_ticker_does_not_mask_stale_private_data(
        self, db, seeded_run_account, private_coverage, strat,
        live_check_config, monkeypatch,
    ):
        """Port of audit test_fresh_public_ticker_masks_stale_private_data
        (cad63f4), inverted: a fresh ticker, day-old position rows and an
        execution after them → SKIP, never a green mark. The run has a
        covering session and no gaps, so only the stale anchor can SKIP."""
        # exec e1 at now-30m on the run's account, fresh ticker
        _seed_window_data(db, _NOW, account_id=seeded_run_account.account_id)
        old = _NOW - timedelta(hours=20)
        with db.get_session() as session:
            for side in ("Buy", "Sell"):
                session.add(PositionSnapshot(
                    run_id="test-run-id",
                    account_id=seeded_run_account.account_id,
                    symbol="LTCUSDT", exchange_ts=old, local_ts=old,
                    side=side, size=Decimal("0"), entry_price=Decimal("0"),
                    unrealised_pnl=Decimal("0"), source="live",
                ))

        def _boom(*args, **kwargs):
            raise AssertionError("no verdict on stale private data")

        monkeypatch.setattr(lc_main.runner, "run_strat", _boom)
        lines = lc_main.watch_tick(
            live_check_config, db, seeded_run_account.run_id,
            seeded_run_account.account_id, timedelta(hours=1), _LAG,
            staleness_threshold(_LAG), now=_NOW,
        )
        assert len(lines) == 1
        assert "SKIP" in lines[0]
        assert "✓" not in lines[0]
        assert "e1" in lines[0]

    def test_missing_anchor_renders_skip_line(
        self, db, seeded_run_account, private_coverage, strat,
        live_check_config, monkeypatch,
    ):
        """Covered window but no position rows at all → SKIP line."""
        _seed_window_data(db, _NOW)

        def _boom(*args, **kwargs):
            raise AssertionError("no verdict without end anchors")

        monkeypatch.setattr(lc_main.runner, "run_strat", _boom)
        lines = lc_main.watch_tick(
            live_check_config, db, seeded_run_account.run_id,
            seeded_run_account.account_id, timedelta(hours=1), _LAG,
            staleness_threshold(_LAG), now=_NOW,
        )
        assert len(lines) == 1
        assert "SKIP" in lines[0]
        assert "position row" in lines[0]


class TestWatchWindowOverride:
    def test_cli_last_reaches_reconcile_window(
        self, db, seeded_run_account, private_coverage, fit_anchors, strat,
        live_check_config, monkeypatch,
    ):
        """The `last` passed into watch_tick sizes the window — a --last
        override must NOT be silently replaced by config.last (4h default)."""
        fit_anchors(_NOW - timedelta(minutes=5))
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
