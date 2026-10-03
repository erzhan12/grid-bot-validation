"""End-of-window position anchor fitness (feature 0110 B2c)."""

from datetime import UTC, timedelta
from decimal import Decimal

import pytest

from grid_db import Order, PositionSnapshot, PrivateExecution
from grid_db.models import Run

from live_check import ground_truth
from live_check.window import Window

RUN_ID = "test-run-id"
_SYMBOL = "LTCUSDT"


def _ms(ts) -> str:
    """Bybit ms timestamp of a naive-UTC datetime."""
    return str(int(ts.replace(tzinfo=UTC).timestamp() * 1000))


def _add_position(db, acc, side, *, exchange_ts, local_ts=None, size="0",
                  unrealised=None, raw_json=None, updated=None):
    """One live position row; raw_json carries Bybit's updatedTime unless
    given explicitly (as the WS writer and the REST snapshot store it)."""
    if raw_json is None:
        raw_json = {"updatedTime": _ms(updated or exchange_ts)}
    with db.get_session() as session:
        session.add(PositionSnapshot(
            run_id=RUN_ID,
            account_id=acc,
            symbol=_SYMBOL,
            exchange_ts=exchange_ts,
            local_ts=local_ts if local_ts is not None else exchange_ts,
            side=side,
            size=Decimal(size),
            entry_price=Decimal("80"),
            unrealised_pnl=Decimal(unrealised) if unrealised is not None else None,
            source="live",
            raw_json=raw_json,
        ))


def _add_exec(db, acc, exec_id, ts, *, order_id=None, side="Buy",
              run_id=RUN_ID):
    with db.get_session() as session:
        session.add(PrivateExecution(
            run_id=run_id,
            account_id=acc,
            symbol=_SYMBOL,
            exec_id=exec_id,
            order_id=order_id or f"o-{exec_id}",
            exchange_ts=ts,
            side=side,
            exec_price=Decimal("80"),
            exec_qty=Decimal("0.2"),
            exec_fee=Decimal("0.01"),
            closed_pnl=Decimal("0"),
        ))


def _add_order(db, acc, order_id, ts, *, side, reduce_only, run_id=RUN_ID):
    with db.get_session() as session:
        session.add(Order(
            run_id=run_id,
            account_id=acc,
            order_id=order_id,
            symbol=_SYMBOL,
            exchange_ts=ts,
            local_ts=ts,
            status="Filled",
            side=side,
            price=Decimal("80"),
            qty=Decimal("0.2"),
            leaves_qty=Decimal("0"),
            reduce_only=reduce_only,
        ))


def _window(ts) -> Window:
    return Window(start=ts - timedelta(hours=1), end=ts)


def _reason(db, acc, window):
    with db.get_readonly_session() as session:
        anchors = ground_truth.end_anchors(
            session, RUN_ID, acc, _SYMBOL, window.end
        )
        return ground_truth.end_anchor_skip_reason(
            session, RUN_ID, acc, _SYMBOL, window, anchors
        )


@pytest.fixture
def acc(seeded_run_account):
    return seeded_run_account.account_id


def _flat_pair(db, acc, ts):
    for side in ("Buy", "Sell"):
        _add_position(db, acc, side, exchange_ts=ts)


class TestRowFitness:
    def test_flat_pair_is_fit(self, db, acc, ts):
        """Both legs present and flat, no executions → fit."""
        _flat_pair(db, acc, ts - timedelta(minutes=5))
        assert _reason(db, acc, _window(ts)) is None

    def test_missing_leg_is_unfit(self, db, acc, ts):
        """No row for a leg → SKIP naming the leg."""
        _add_position(db, acc, "Buy", exchange_ts=ts - timedelta(minutes=5))
        reason = _reason(db, acc, _window(ts))
        assert "short" in reason

    def test_row_after_window_end_is_not_an_anchor(self, db, acc, ts):
        """A row received after window.end is not evidence for the window."""
        _add_position(db, acc, "Buy", exchange_ts=ts - timedelta(minutes=5))
        _add_position(db, acc, "Sell", exchange_ts=ts - timedelta(minutes=5),
                      local_ts=ts + timedelta(seconds=1))
        assert "short" in _reason(db, acc, _window(ts))

    @pytest.mark.parametrize("marker", ["rest_failure", "malformed",
                                        "empty_response"])
    def test_unfit_placeholder(self, db, acc, ts, marker):
        """Startup placeholders without a successful leg reading are unfit."""
        _add_position(db, acc, "Buy", exchange_ts=ts - timedelta(minutes=5),
                      raw_json={"synthetic": marker})
        _add_position(db, acc, "Sell", exchange_ts=ts - timedelta(minutes=5))
        reason = _reason(db, acc, _window(ts))
        assert marker in reason
        assert "insufficient position evidence" in reason

    def test_absent_side_placeholder_is_fit(self, db, acc, ts):
        """absent_side comes from a successful fetch: a known flat leg."""
        _add_position(db, acc, "Buy", exchange_ts=ts - timedelta(minutes=5),
                      raw_json={"synthetic": "absent_side"})
        _add_position(db, acc, "Sell", exchange_ts=ts - timedelta(minutes=5))
        assert _reason(db, acc, _window(ts)) is None

    def test_open_leg_without_unrealised_is_unfit(self, db, acc, ts):
        """size > 0 with NULL unrealised_pnl → SKIP."""
        _add_position(db, acc, "Buy", exchange_ts=ts - timedelta(minutes=5),
                      size="0.2")
        _add_position(db, acc, "Sell", exchange_ts=ts - timedelta(minutes=5))
        assert "unrealised" in _reason(db, acc, _window(ts))

    def test_open_leg_with_unrealised_is_fit(self, db, acc, ts):
        """size > 0 with a known unrealised_pnl → fit."""
        _add_position(db, acc, "Buy", exchange_ts=ts - timedelta(minutes=5),
                      size="0.2", unrealised="0.4")
        _add_position(db, acc, "Sell", exchange_ts=ts - timedelta(minutes=5))
        assert _reason(db, acc, _window(ts)) is None

    def test_newer_unfit_row_does_not_fall_back(self, db, acc, ts):
        """The newest received row is judged; an older fit row is ignored."""
        _flat_pair(db, acc, ts - timedelta(minutes=10))
        _add_position(db, acc, "Buy", exchange_ts=ts - timedelta(minutes=5),
                      raw_json={"synthetic": "rest_failure"})
        assert "rest_failure" in _reason(db, acc, _window(ts))

    def test_local_ts_tie_takes_later_insert(self, db, acc, ts):
        """Rows tied on local_ts resolve to the later insert (id)."""
        at = ts - timedelta(minutes=5)
        _add_position(db, acc, "Buy", exchange_ts=at)
        _add_position(db, acc, "Buy", exchange_ts=at, size="0.2")  # unfit
        _add_position(db, acc, "Sell", exchange_ts=at)
        assert "unrealised" in _reason(db, acc, _window(ts))


class TestExecutionsAfterAnchor:
    """Bybit pushes a position update after every fill, with updatedTime at
    or after the fill's execTime; a fill on a leg after that leg's anchor
    means the anchor missed it."""

    def test_fill_one_ms_after_anchor_is_unfit(self, db, acc, ts):
        """A long-leg fill 1 ms after the long anchor → SKIP."""
        at = ts - timedelta(minutes=5)
        _flat_pair(db, acc, at)
        _add_order(db, acc, "o1", at, side="Buy", reduce_only=False)
        _add_exec(db, acc, "e1", at + timedelta(milliseconds=1), order_id="o1")
        reason = _reason(db, acc, _window(ts))
        assert "long" in reason
        assert "e1" in reason

    def test_fill_at_anchor_time_is_fit(self, db, acc, ts):
        """execTime == anchor updatedTime → fit (the push reflects it)."""
        at = ts - timedelta(minutes=5)
        _flat_pair(db, acc, at)
        _add_order(db, acc, "o1", at, side="Buy", reduce_only=False)
        _add_exec(db, acc, "e1", at, order_id="o1")
        assert _reason(db, acc, _window(ts)) is None

    def test_post_fill_push_received_later_is_fit(self, db, acc, ts):
        """The push carrying the fill's updatedTime, received ~0.3 s later."""
        fill = ts - timedelta(minutes=5)
        _add_position(db, acc, "Buy", exchange_ts=fill,
                      local_ts=fill + timedelta(milliseconds=300),
                      size="0.2", unrealised="0")
        _add_position(db, acc, "Sell", exchange_ts=fill - timedelta(hours=1))
        _add_order(db, acc, "o1", fill, side="Buy", reduce_only=False)
        _add_exec(db, acc, "e1", fill, order_id="o1")
        assert _reason(db, acc, _window(ts)) is None

    def test_other_legs_fill_does_not_affect_this_leg(self, db, acc, ts):
        """A short-leg fill after the long anchor leaves the long leg fit."""
        long_at = ts - timedelta(minutes=10)
        short_at = ts - timedelta(minutes=5)
        _add_position(db, acc, "Buy", exchange_ts=long_at)
        _add_position(db, acc, "Sell", exchange_ts=short_at)
        _add_order(db, acc, "o1", short_at, side="Sell", reduce_only=False)
        _add_exec(db, acc, "e1", short_at, order_id="o1", side="Sell")
        assert _reason(db, acc, _window(ts)) is None

    @pytest.mark.parametrize("side,reduce_only,leg", [
        ("Buy", False, "long"), ("Sell", True, "long"),
        ("Sell", False, "short"), ("Buy", True, "short"),
    ])
    def test_side_and_reduce_only_pick_the_leg(
        self, db, acc, ts, side, reduce_only, leg
    ):
        """Hedge-mode leg from order side + reduce_only."""
        at = ts - timedelta(minutes=5)
        _flat_pair(db, acc, at)
        _add_order(db, acc, "o1", at, side=side, reduce_only=reduce_only)
        _add_exec(db, acc, "e1", at + timedelta(seconds=1), order_id="o1",
                  side=side)
        reason = _reason(db, acc, _window(ts))
        assert reason.startswith(leg)

    def test_unknown_order_counts_against_both_legs(self, db, acc, ts):
        """No order row (e.g. a manual market close — only Limit orders are
        recorded) → the fill counts against both legs, so the untouched leg
        whose updatedTime is older SKIPs too."""
        long_at = ts - timedelta(minutes=10)
        fill = ts - timedelta(minutes=6)
        _add_position(db, acc, "Buy", exchange_ts=long_at)
        _add_position(db, acc, "Sell", exchange_ts=fill)
        _add_exec(db, acc, "e1", fill, side="Sell")
        assert "long" in _reason(db, acc, _window(ts))

    def test_null_reduce_only_counts_against_both_legs(self, db, acc, ts):
        """reduce_only NULL (pre-0029 row) → leg unknown → both legs."""
        long_at = ts - timedelta(minutes=10)
        fill = ts - timedelta(minutes=6)
        _add_position(db, acc, "Buy", exchange_ts=long_at)
        _add_position(db, acc, "Sell", exchange_ts=fill)
        _add_order(db, acc, "o1", fill, side="Sell", reduce_only=None)
        _add_exec(db, acc, "e1", fill, order_id="o1", side="Sell")
        assert "long" in _reason(db, acc, _window(ts))

    def test_order_id_from_another_run_or_account_is_ignored(
        self, db, acc, ts
    ):
        """An order row with the same id elsewhere is not this order."""
        with db.get_session() as session:
            run = session.get(Run, RUN_ID)
            session.add(Run(
                run_id="other-run", user_id=run.user_id,
                account_id=run.account_id, strategy_id=run.strategy_id,
                run_type="recording", start_ts=run.start_ts,
            ))
        long_at = ts - timedelta(minutes=10)
        fill = ts - timedelta(minutes=6)
        _add_position(db, acc, "Buy", exchange_ts=long_at)
        _add_position(db, acc, "Sell", exchange_ts=fill)
        _add_order(db, acc, "o1", fill, side="Sell", reduce_only=False,
                   run_id="other-run")
        _add_order(db, "other-account", "o1", fill, side="Sell",
                   reduce_only=False)
        _add_exec(db, acc, "e1", fill, order_id="o1", side="Sell")
        assert "long" in _reason(db, acc, _window(ts))

    def test_execution_of_another_account_is_ignored(self, db, acc, ts):
        """Executions are tenant-scoped: another account's fill in the same
        run does not make this account's anchors unfit."""
        at = ts - timedelta(minutes=5)
        _flat_pair(db, acc, at)
        _add_exec(db, "other-account", "e1", at + timedelta(seconds=1))
        assert _reason(db, acc, _window(ts)) is None

    def test_latest_order_row_decides(self, db, acc, ts):
        """Several rows for one order: the latest (exchange_ts, id) decides."""
        long_at = ts - timedelta(minutes=10)
        fill = ts - timedelta(minutes=6)
        _add_position(db, acc, "Buy", exchange_ts=long_at)
        _add_position(db, acc, "Sell", exchange_ts=fill)
        _add_order(db, acc, "o1", fill - timedelta(seconds=1), side="Sell",
                   reduce_only=None)
        _add_order(db, acc, "o1", fill, side="Sell", reduce_only=False)
        _add_exec(db, acc, "e1", fill, order_id="o1", side="Sell")
        assert _reason(db, acc, _window(ts)) is None

    def test_order_row_written_after_its_execution_still_counts(
        self, db, acc, ts
    ):
        """The join is by order id, not time: a late order row still names
        the leg."""
        long_at = ts - timedelta(minutes=10)
        fill = ts - timedelta(minutes=6)
        _add_position(db, acc, "Buy", exchange_ts=long_at)
        _add_position(db, acc, "Sell", exchange_ts=fill)
        _add_exec(db, acc, "e1", fill, order_id="o1", side="Sell")
        _add_order(db, acc, "o1", fill + timedelta(seconds=2), side="Sell",
                   reduce_only=False)
        assert _reason(db, acc, _window(ts)) is None

    def test_fill_before_window_start_but_after_anchor_counts(
        self, db, acc, ts
    ):
        """The anchor must postdate every fill on its leg, not only the
        window's: an older anchor that missed a pre-window fill is stale."""
        window = _window(ts)
        at = window.start - timedelta(minutes=30)
        _flat_pair(db, acc, at)
        _add_order(db, acc, "o1", at, side="Buy", reduce_only=False)
        _add_exec(db, acc, "e1", window.start - timedelta(minutes=10),
                  order_id="o1")
        assert "long" in _reason(db, acc, window)

    @pytest.mark.parametrize("raw_json", [
        {}, {"updatedTime": ""}, {"updatedTime": "not-a-number"},
        {"synthetic": "absent_side"},
    ])
    def test_anchor_time_falls_back_to_exchange_ts(
        self, db, acc, ts, raw_json
    ):
        """No usable updatedTime → the row's exchange_ts is the anchor time:
        a fill after it is caught, one before it is not."""
        at = ts - timedelta(minutes=5)
        _add_position(db, acc, "Buy", exchange_ts=at, raw_json=raw_json)
        _add_position(db, acc, "Sell", exchange_ts=at)
        _add_order(db, acc, "o1", at, side="Buy", reduce_only=False)
        _add_exec(db, acc, "e1", at - timedelta(seconds=1), order_id="o1")
        assert _reason(db, acc, _window(ts)) is None
        _add_exec(db, acc, "e2", at + timedelta(seconds=1), order_id="o1")
        assert "e2" in _reason(db, acc, _window(ts))

    def test_fill_after_window_end_is_ignored(self, db, acc, ts):
        """A fill after window.end is not part of this verdict."""
        at = ts - timedelta(minutes=5)
        _flat_pair(db, acc, at)
        _add_order(db, acc, "o1", at, side="Buy", reduce_only=False)
        _add_exec(db, acc, "e1", ts + timedelta(seconds=1), order_id="o1")
        assert _reason(db, acc, _window(ts)) is None

    def test_anchor_time_is_bybit_updated_time_not_local_fetch_time(
        self, db, acc, ts
    ):
        """A REST startup row stores the local fetch time as exchange_ts;
        fitness compares Bybit's updatedTime from raw_json, so a local clock
        running ahead cannot hide a later fill."""
        updated = ts - timedelta(minutes=10)
        fill = ts - timedelta(minutes=8)
        local_fetch = ts - timedelta(minutes=7)  # clock ahead: after the fill
        _add_position(db, acc, "Buy", exchange_ts=local_fetch,
                      updated=updated)
        _add_position(db, acc, "Sell", exchange_ts=local_fetch,
                      updated=updated)
        _add_order(db, acc, "o1", fill, side="Buy", reduce_only=False)
        _add_exec(db, acc, "e1", fill, order_id="o1")
        assert "long" in _reason(db, acc, _window(ts))

    def test_two_fills_in_one_millisecond_rely_on_the_second_push(
        self, db, acc, ts
    ):
        """Documented residual: two fills sharing a millisecond are only
        caught when the second fill's push is received before window.end."""
        fill = ts - timedelta(minutes=5)
        _add_position(db, acc, "Buy", exchange_ts=fill, size="0.2",
                      unrealised="0")
        _add_position(db, acc, "Buy", exchange_ts=fill, size="0.4",
                      unrealised="0", local_ts=fill + timedelta(seconds=1))
        _add_position(db, acc, "Sell", exchange_ts=fill)
        for exec_id in ("e1", "e2"):
            _add_order(db, acc, f"o-{exec_id}", fill, side="Buy",
                       reduce_only=False)
            _add_exec(db, acc, exec_id, fill)
        with db.get_readonly_session() as session:
            anchors = ground_truth.end_anchors(
                session, RUN_ID, acc, _SYMBOL, ts
            )
            assert anchors["Buy"].size == Decimal("0.4")
        assert _reason(db, acc, _window(ts)) is None
