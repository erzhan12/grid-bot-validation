"""Private-stream coverage gate (feature 0110 B2b)."""

from datetime import UTC, timedelta
from decimal import Decimal

import pytest

from grid_db import (
    PositionSnapshot,
    PrivateStreamGap,
    PrivateStreamSession,
    RecoveryStatus,
    WalletSnapshot,
)
from grid_db.models import Order, Run

from live_check import ground_truth
from live_check.window import Window

RUN_ID = "test-run-id"
_SYMBOL = "LTCUSDT"


def _window(ts) -> Window:
    return Window(start=ts - timedelta(hours=1), end=ts)


def _add_session(db, account_id, connected_at, checkpoint, run_id=RUN_ID):
    with db.get_session() as session:
        session.add(PrivateStreamSession(
            run_id=run_id,
            account_id=account_id,
            connected_at=connected_at,
            last_checkpoint_ts=checkpoint,
        ))


def _add_gap(db, account_id, gap_start, gap_end, *, run_id=RUN_ID,
             symbol=_SYMBOL, status=RecoveryStatus.RECOVERED):
    with db.get_session() as session:
        session.add(PrivateStreamGap(
            run_id=run_id,
            account_id=account_id,
            symbol=symbol,
            gap_start=gap_start,
            gap_end=gap_end,
            recovery_status=status,
            inserted=0,
            duplicates=0,
        ))


def _add_position(db, account_id, side, ts):
    with db.get_session() as session:
        session.add(PositionSnapshot(
            run_id=RUN_ID,
            account_id=account_id,
            symbol=_SYMBOL,
            exchange_ts=ts,
            local_ts=ts,
            side=side,
            size=Decimal("0.2"),
            entry_price=Decimal("80"),
            source="live",
        ))


def _add_wallet(db, account_id, ts):
    with db.get_session() as session:
        session.add(WalletSnapshot(
            run_id=RUN_ID,
            account_id=account_id,
            exchange_ts=ts,
            local_ts=ts,
            coin="USDT",
            wallet_balance=Decimal("1000"),
            available_balance=Decimal("1000"),
        ))


def _reason(db, account_id, window, symbol=_SYMBOL):
    with db.get_readonly_session() as session:
        return ground_truth.private_coverage_skip_reason(
            session, RUN_ID, account_id, symbol, window
        )


@pytest.fixture
def acc(seeded_run_account):
    return seeded_run_account.account_id


class TestSessionCoverage:
    def test_covering_session_without_gaps_passes(self, db, acc, ts):
        """A session connected before and checkpointed after the window → None."""
        _add_session(db, acc, ts - timedelta(days=1), ts + timedelta(minutes=1))
        assert _reason(db, acc, _window(ts)) is None

    def test_exact_session_bounds_pass(self, db, acc, ts):
        """connected_at == interval start and checkpoint == interval end pass."""
        window = _window(ts)
        _add_session(db, acc, window.start, window.end)
        assert _reason(db, acc, window) is None

    def test_no_session_skips(self, db, acc, ts):
        """No session row for the run → SKIP."""
        reason = _reason(db, acc, _window(ts))
        assert reason is not None
        assert "no private-stream session" in reason

    def test_session_connected_after_interval_start_skips(self, db, acc, ts):
        """A session that starts inside the window does not cover it."""
        window = _window(ts)
        _add_session(db, acc, window.start + timedelta(seconds=1),
                     ts + timedelta(minutes=1))
        reason = _reason(db, acc, window)
        assert reason is not None
        assert "not covered" in reason

    def test_checkpoint_before_interval_end_skips(self, db, acc, ts):
        """A checkpoint short of the window end does not cover it."""
        window = _window(ts)
        _add_session(db, acc, ts - timedelta(days=1),
                     window.end - timedelta(seconds=1))
        reason = _reason(db, acc, window)
        assert reason is not None
        assert "not covered" in reason

    def test_two_sessions_that_only_together_cover_skip(self, db, acc, ts):
        """Back-to-back sessions (a recorder restart) do not cover: the hole
        between them has no checkpoint."""
        window = _window(ts)
        middle = window.start + timedelta(minutes=30)
        _add_session(db, acc, ts - timedelta(days=1), middle)
        _add_session(db, acc, middle, ts + timedelta(minutes=1))
        assert "not covered" in _reason(db, acc, window)

    def test_later_session_alone_covering_passes(self, db, acc, ts):
        """Any one session may cover the interval, not only the first."""
        window = _window(ts)
        _add_session(db, acc, ts - timedelta(days=2), ts - timedelta(days=1))
        _add_session(db, acc, window.start - timedelta(minutes=1),
                     ts + timedelta(minutes=1))
        assert _reason(db, acc, window) is None

    def test_aware_utc_rows_compare_as_naive_utc(self, db, acc, ts):
        """The recorder writes aware UTC times; SQLite returns them naive."""
        window = _window(ts)
        _add_session(db, acc, (window.start - timedelta(hours=1)).replace(
            tzinfo=UTC), (window.end + timedelta(seconds=1)).replace(tzinfo=UTC))
        assert _reason(db, acc, window) is None
        _add_gap(db, acc, window.end.replace(tzinfo=UTC), None)
        assert "open" in _reason(db, acc, window)

    def test_session_of_another_account_is_ignored(self, db, acc, ts):
        """A covering session for a different account does not count."""
        _add_session(db, "other-account", ts - timedelta(days=1),
                     ts + timedelta(minutes=1))
        assert _reason(db, acc, _window(ts)) is not None


class TestGaps:
    @pytest.fixture(autouse=True)
    def _covered(self, db, acc, ts):
        _add_session(db, acc, ts - timedelta(days=1), ts + timedelta(minutes=1))

    def test_open_gap_skips(self, db, acc, ts):
        """An open gap (gap_end NULL) that started before the end → SKIP."""
        _add_gap(db, acc, ts - timedelta(hours=2), None,
                 status=RecoveryStatus.PENDING)
        reason = _reason(db, acc, _window(ts))
        assert reason is not None
        assert "open" in reason

    @pytest.mark.parametrize("status", list(RecoveryStatus))
    def test_overlapping_gap_skips_for_every_status(self, db, acc, ts, status):
        """Any overlapping gap SKIPs, recovered or not; status is reported."""
        start = ts - timedelta(minutes=30)
        end = ts - timedelta(minutes=29)
        _add_gap(db, acc, start, end, status=status)
        reason = _reason(db, acc, _window(ts))
        assert reason is not None
        assert str(status) in reason
        assert start.isoformat(sep=" ") in reason

    def test_open_gap_reported_even_when_checkpoint_froze(self, db, acc, ts):
        """The recorder stops checkpointing while a gap is open; the reason
        still names the gap (bounds + status), not just "not covered"."""
        window = _window(ts)
        with db.get_session() as session:
            session.query(PrivateStreamSession).update(
                {"last_checkpoint_ts": window.start + timedelta(minutes=10)}
            )
        _add_gap(db, acc, window.start + timedelta(minutes=11), None,
                 status=RecoveryStatus.PENDING)
        reason = _reason(db, acc, window)
        assert "gap" in reason
        assert "open" in reason
        assert "pending" in reason

    def test_several_gaps_report_oldest_and_count(self, db, acc, ts):
        """Two overlapping gaps: the reason names the oldest and '(+1 more)'."""
        first = ts - timedelta(minutes=50)
        _add_gap(db, acc, ts - timedelta(minutes=20), ts - timedelta(minutes=19))
        _add_gap(db, acc, first, first + timedelta(minutes=1))
        reason = _reason(db, acc, _window(ts))
        assert first.isoformat(sep=" ") in reason
        assert "(+1 more)" in reason

    def test_seed_stretch_hint_comes_after_count(self, db, acc, ts):
        """Two seed-stretch gaps: the count precedes the restart hint."""
        window = _window(ts)
        _add_position(db, acc, "Sell", window.start - timedelta(minutes=30))
        for minutes in (20, 10):
            start = window.start - timedelta(minutes=minutes)
            _add_gap(db, acc, start, start + timedelta(minutes=1))
        reason = _reason(db, acc, window)
        assert reason.index("(+1 more)") < reason.index("post-gap snapshot")

    def test_gap_inside_window_has_no_restart_hint(self, db, acc, ts):
        """A gap inside the window is an ordinary SKIP, no restart hint."""
        _add_gap(db, acc, ts - timedelta(minutes=30), ts - timedelta(minutes=29))
        assert "restart" not in _reason(db, acc, _window(ts))

    def test_gap_ending_exactly_at_interval_start_skips(self, db, acc, ts):
        """gap_end == interval start overlaps."""
        window = _window(ts)
        _add_gap(db, acc, window.start - timedelta(minutes=5), window.start)
        assert _reason(db, acc, window) is not None

    def test_gap_starting_exactly_at_interval_end_skips(self, db, acc, ts):
        """gap_start == interval end overlaps."""
        window = _window(ts)
        _add_gap(db, acc, window.end, window.end + timedelta(minutes=5))
        assert _reason(db, acc, window) is not None

    def test_gap_before_interval_is_ignored(self, db, acc, ts):
        """A gap that ended before the interval start does not matter."""
        window = _window(ts)
        _add_gap(db, acc, window.start - timedelta(minutes=10),
                 window.start - timedelta(seconds=1))
        assert _reason(db, acc, window) is None

    def test_gap_after_interval_is_ignored(self, db, acc, ts):
        """A gap that started after the interval end does not matter."""
        window = _window(ts)
        _add_gap(db, acc, window.end + timedelta(seconds=1),
                 window.end + timedelta(minutes=5))
        assert _reason(db, acc, window) is None

    def test_gap_of_other_symbol_or_account_is_ignored(self, db, acc, ts):
        """Gap rows for another symbol or account do not affect this strat."""
        inside = (ts - timedelta(minutes=30), ts - timedelta(minutes=29))
        _add_gap(db, acc, *inside, symbol="SOLUSDT")
        _add_gap(db, "other-account", *inside)
        assert _reason(db, acc, _window(ts)) is None

    def test_gap_of_other_run_is_ignored(self, db, acc, ts):
        """Gap rows of another run do not affect this run."""
        with db.get_session() as session:
            run = session.get(Run, RUN_ID)
            session.add(Run(
                run_id="other-run",
                user_id=run.user_id,
                account_id=run.account_id,
                strategy_id=run.strategy_id,
                run_type="recording",
                start_ts=run.start_ts,
            ))
        _add_gap(db, acc, ts - timedelta(minutes=30),
                 ts - timedelta(minutes=29), run_id="other-run")
        assert _reason(db, acc, _window(ts)) is None


def _add_active_order(db, account_id, order_id, ts, *, run_id=RUN_ID,
                      symbol=_SYMBOL):
    with db.get_session() as session:
        session.add(Order(
            run_id=run_id, account_id=account_id, order_id=order_id,
            symbol=symbol, exchange_ts=ts, local_ts=ts, status="New",
            side="Buy", price=Decimal("80"), qty=Decimal("0.2"),
            leaves_qty=Decimal("0.2"), reduce_only=False,
        ))


class TestOrdersAcrossAGap:
    """PR #291 review: B3 moves the position/wallet seeds past a gap, but
    orders are not backfilled — an active order that last updated before an
    earlier gap's end may have filled or been cancelled during it."""

    def _post_gap(self, db, acc, ts):
        window = _window(ts)
        _add_session(db, acc, ts - timedelta(days=1), ts + timedelta(minutes=1))
        gap_end = window.start - timedelta(minutes=10)
        _add_gap(db, acc, window.start - timedelta(minutes=15), gap_end)
        snapshot = gap_end + timedelta(seconds=2)
        for side in ("Buy", "Sell"):
            with db.get_session() as session:
                session.add(PositionSnapshot(
                    run_id=RUN_ID, account_id=acc, symbol=_SYMBOL,
                    exchange_ts=snapshot, local_ts=snapshot, side=side,
                    size=Decimal("0"), entry_price=Decimal("0"), source="live",
                ))
        _add_wallet(db, acc, snapshot)
        return window, gap_end

    def test_order_resting_across_a_gap_skips(self, db, acc, ts):
        """Post-gap position rows, but an active order last updated before
        the gap ended → SKIP naming the order."""
        window, gap_end = self._post_gap(db, acc, ts)
        _add_active_order(db, acc, "o-old", gap_end - timedelta(minutes=30))
        reason = _reason(db, acc, window)
        assert reason is not None
        assert "o-old" in reason

    def test_orders_placed_after_the_gap_pass(self, db, acc, ts):
        """Every active order updated after the gap → covered."""
        window, gap_end = self._post_gap(db, acc, ts)
        _add_active_order(db, acc, "o-new", gap_end + timedelta(seconds=5))
        assert _reason(db, acc, window) is None

    @pytest.mark.parametrize("scope", [
        {"symbol": "BTCUSDT"}, {"account_id": "other-account"},
        {"run_id": "other-run"},
    ])
    def test_order_of_other_scope_is_ignored(self, db, acc, ts, scope):
        """An old active order of another symbol / account / run does not
        SKIP this run's window."""
        window, gap_end = self._post_gap(db, acc, ts)
        with db.get_session() as session:
            run = session.get(Run, RUN_ID)
            session.add(Run(
                run_id="other-run", user_id=run.user_id,
                account_id=run.account_id, strategy_id=run.strategy_id,
                run_type="recording", start_ts=run.start_ts,
            ))
        account_id = scope.get("account_id", acc)
        _add_active_order(
            db, account_id, "o-other", gap_end - timedelta(minutes=30),
            run_id=scope.get("run_id", RUN_ID),
            symbol=scope.get("symbol", _SYMBOL),
        )
        assert _reason(db, acc, window) is None


class TestSeedAnchors:
    """Replay seeds from rows at-or-before window.start; the interval must
    reach back to them, since a gap after a seed row changes the seed."""

    def test_gap_between_position_seed_and_window_skips(self, db, acc, ts):
        """A gap after the position seed row but before window.start SKIPs."""
        window = _window(ts)
        _add_session(db, acc, ts - timedelta(days=1), ts + timedelta(minutes=1))
        gap = (window.start - timedelta(minutes=10),
               window.start - timedelta(minutes=5))
        _add_gap(db, acc, *gap)
        assert _reason(db, acc, window) is None  # no seed row yet
        _add_position(db, acc, "Sell", window.start - timedelta(minutes=30))
        assert _reason(db, acc, window) is not None

    def test_gap_only_before_window_tells_operator_to_restart(
        self, db, acc, ts
    ):
        """A gap wholly between a seed row and window.start SKIPs every later
        window of the run; the reason says so and names the remedy."""
        window = _window(ts)
        _add_session(db, acc, ts - timedelta(days=1), ts + timedelta(minutes=1))
        _add_gap(db, acc, window.start - timedelta(minutes=10),
                 window.start - timedelta(minutes=5))
        _add_position(db, acc, "Sell", window.start - timedelta(minutes=30))
        reason = _reason(db, acc, window)
        assert "only touch the seed rows" in reason
        assert "post-gap snapshot did not land" in reason
        assert "recorder restarts" in reason

    def test_post_gap_snapshot_moves_the_interval_past_the_gap(
        self, db, acc, ts
    ):
        """0110 B3: the recorder's post-gap REST rows (exchange_ts = local_ts
        = snapshot time, after gap_end) become the seed of later windows, so
        those are covered again; a window starting before them still SKIPs."""
        window = _window(ts)
        _add_session(db, acc, ts - timedelta(days=1), ts + timedelta(minutes=1))
        gap_end = window.start - timedelta(minutes=5)
        _add_gap(db, acc, window.start - timedelta(minutes=10), gap_end)
        _add_position(db, acc, "Sell", window.start - timedelta(minutes=30))
        assert _reason(db, acc, window) is not None
        snapshot = gap_end + timedelta(seconds=2)
        _add_position(db, acc, "Sell", snapshot)
        _add_wallet(db, acc, snapshot)
        assert _reason(db, acc, window) is None
        early = Window(start=gap_end + timedelta(seconds=1), end=ts)
        assert _reason(db, acc, early) is not None

    def test_wallet_seed_extends_interval(self, db, acc, ts):
        """The USDT wallet seed row extends the interval too."""
        window = _window(ts)
        _add_session(db, acc, ts - timedelta(days=1), ts + timedelta(minutes=1))
        _add_gap(db, acc, window.start - timedelta(minutes=10),
                 window.start - timedelta(minutes=5))
        _add_wallet(db, acc, window.start - timedelta(minutes=30))
        assert _reason(db, acc, window) is not None

    def test_seed_row_from_startup_race_is_clamped_to_connect(
        self, db, acc, ts
    ):
        """The recorder stamps connected_at after the subscription acks, so a
        push in that window lands just before the session row; within a run
        it can come from nothing else, so its anchor is the connect time."""
        window = _window(ts)
        connected = window.start - timedelta(minutes=5)
        _add_session(db, acc, connected, ts + timedelta(minutes=1))
        _add_position(db, acc, "Buy", connected - timedelta(milliseconds=300))
        assert _reason(db, acc, window) is None

    def test_window_before_session_still_skips(self, db, acc, ts):
        """The clamp applies to seed rows only, never to the window itself."""
        window = _window(ts)
        _add_session(db, acc, window.start + timedelta(minutes=1),
                     ts + timedelta(minutes=1))
        _add_position(db, acc, "Buy", window.start - timedelta(minutes=30))
        assert "not covered" in _reason(db, acc, window)

    def test_seed_anchor_uses_receipt_time_not_exchange_time(
        self, db, acc, ts
    ):
        """A quiet leg's push keeps an old updatedTime (exchange_ts before the
        session); it is anchored on local_ts, so the window stays covered."""
        window = _window(ts)
        _add_session(db, acc, window.start - timedelta(minutes=5),
                     ts + timedelta(minutes=1))
        with db.get_session() as session:
            session.add(PositionSnapshot(
                run_id=RUN_ID,
                account_id=acc,
                symbol=_SYMBOL,
                exchange_ts=window.start - timedelta(days=3),
                local_ts=window.start - timedelta(minutes=1),
                side="Buy",
                size=Decimal("0.2"),
                entry_price=Decimal("80"),
                source="live",
            ))
        assert _reason(db, acc, window) is None

    def test_gap_reached_only_by_an_end_anchor_skips(self, db, acc, ts):
        """0110 B2c: the end anchor (latest row RECEIVED by window.end) also
        extends the interval — here a row stamped by Bybit after
        window.start (so not a seed row) but received before it."""
        window = _window(ts)
        _add_session(db, acc, ts - timedelta(days=1), ts + timedelta(minutes=1))
        _add_gap(db, acc, window.start - timedelta(minutes=10),
                 window.start - timedelta(minutes=5))
        assert _reason(db, acc, window) is None
        with db.get_session() as session:
            session.add(PositionSnapshot(
                run_id=RUN_ID, account_id=acc, symbol=_SYMBOL,
                exchange_ts=window.start + timedelta(seconds=1),
                local_ts=window.start - timedelta(minutes=30),
                side="Buy", size=Decimal("0"), entry_price=Decimal("0"),
                source="live",
            ))
        assert _reason(db, acc, window) is not None

    def test_end_anchor_before_session_is_clamped(self, db, acc, ts):
        """End anchors are clamped to the run's first connected_at too."""
        window = _window(ts)
        connected = window.start - timedelta(minutes=5)
        _add_session(db, acc, connected, ts + timedelta(minutes=1))
        with db.get_session() as session:
            session.add(PositionSnapshot(
                run_id=RUN_ID, account_id=acc, symbol=_SYMBOL,
                exchange_ts=window.start + timedelta(seconds=1),
                local_ts=connected - timedelta(milliseconds=300),
                side="Buy", size=Decimal("0"), entry_price=Decimal("0"),
                source="live",
            ))
        assert _reason(db, acc, window) is None

    def test_seed_row_after_window_start_is_not_a_seed(self, db, acc, ts):
        """A row inside the window is not a seed row and does not extend it."""
        window = _window(ts)
        _add_session(db, acc, window.start, ts + timedelta(minutes=1))
        _add_position(db, acc, "Buy", window.start + timedelta(minutes=1))
        assert _reason(db, acc, window) is None


class TestPre0110:
    def test_missing_coverage_tables_skip(self, db, acc, ts):
        """A recording without the 0110 tables SKIPs, never raises."""
        PrivateStreamGap.__table__.drop(db.engine)
        PrivateStreamSession.__table__.drop(db.engine)
        reason = _reason(db, acc, _window(ts))
        assert reason == "recorder has no private-stream coverage (pre-0110)"
