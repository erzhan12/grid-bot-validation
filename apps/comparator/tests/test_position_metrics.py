"""Tests for position telemetry pairing and metrics (feature 0034)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from grid_db.models import PositionSnapshot

from comparator.metrics import ValidationMetrics
from comparator.position_loader import (
    PositionTelemetryNotMigratedError,
    load_position_snapshots,
)
from comparator.position_metrics import (
    PAIR_TOLERANCE_S,
    PositionComparator,
)


@pytest.fixture
def base_ts():
    return datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


def _snap(
    side: str,
    ts: datetime,
    size: Decimal = Decimal("1"),
    entry_price: Decimal = Decimal("100"),
    mark_price: Decimal = Decimal("101"),
    liq_price: Decimal = Decimal("90"),
    position_im: Decimal = Decimal("10"),
    position_mm: Decimal = Decimal("0.5"),
    cum_realised_pnl: Decimal = Decimal("0"),
    cur_realised_pnl: Decimal = Decimal("0"),
    unrealised_pnl: Decimal = Decimal("0"),
    position_value: Decimal = Decimal("100"),
    source: str = "live",
) -> PositionSnapshot:
    return PositionSnapshot(
        run_id="run-test",
        account_id="acct-1",
        symbol="LTCUSDT",
        exchange_ts=ts,
        local_ts=ts,
        side=side,
        size=size,
        entry_price=entry_price,
        liq_price=liq_price,
        unrealised_pnl=unrealised_pnl,
        source=source,
        mark_price=mark_price,
        position_im=position_im,
        position_mm=position_mm,
        cum_realised_pnl=cum_realised_pnl,
        cur_realised_pnl=cur_realised_pnl,
        position_value=position_value,
    )


def test_pair_within_tolerance(base_ts):
    """Backtest at T pairs with first live at >= T within 5s."""
    live = [_snap("Buy", base_ts + timedelta(seconds=2), source="live")]
    bt = [_snap("Buy", base_ts, source="backtest")]

    pairs = PositionComparator().pair_and_compare(live, bt)

    assert len(pairs) == 1
    assert pairs[0].live is live[0]
    assert pairs[0].backtest is bt[0]


def test_pair_live_before_backtest_within_tolerance(base_ts):
    """Live may arrive before replay's simulated fill time and still pair."""
    bt_ts = base_ts.replace(hour=12, minute=30, second=26, microsecond=112000)
    live_ts = base_ts.replace(hour=12, minute=30, second=26, microsecond=73000)
    live = [_snap("Buy", live_ts, source="live")]
    bt = [_snap("Buy", bt_ts, source="backtest")]

    pairs = PositionComparator().pair_and_compare(live, bt)

    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    assert len(pairs) == 1
    assert pairs[0].live is live[0]
    assert pairs[0].backtest is bt[0]
    assert metrics.position_pairs_compared == 1
    assert metrics.position_pairs_unmatched_bt == 0


def test_pair_outside_tolerance_unmatched(base_ts):
    """Bt with no live in tolerance window counts as unmatched, no consume."""
    live = [_snap("Buy", base_ts + timedelta(seconds=PAIR_TOLERANCE_S + 5), source="live")]
    bt = [_snap("Buy", base_ts, source="backtest")]

    pairs = PositionComparator().pair_and_compare(live, bt)

    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    assert metrics.position_pairs_compared == 0
    assert metrics.position_pairs_unmatched_bt == 1


def test_monotonic_consume_invariant(base_ts):
    """Two bt rows before a single live row: first pairs, second unmatched.

    Without the consume step, both bt rows would pair to the same live row
    and inflate position_pairs_compared.
    """
    live = [_snap("Buy", base_ts + timedelta(seconds=2), source="live")]
    bt = [
        _snap("Buy", base_ts, source="backtest"),
        _snap("Buy", base_ts + timedelta(seconds=1), source="backtest"),
    ]

    pairs = PositionComparator().pair_and_compare(live, bt)

    matched = [p for p in pairs if p.live is not None]
    unmatched = [p for p in pairs if p.live is None]
    assert len(matched) == 1
    assert len(unmatched) == 1

    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    assert metrics.position_pairs_compared == 1
    assert metrics.position_pairs_unmatched_bt == 1


def test_zero_deltas_when_equal(base_ts):
    """Trivial all-equal pair → all per-field deltas zero."""
    live = [_snap("Buy", base_ts, source="live")]
    bt = [_snap("Buy", base_ts, source="backtest")]

    pairs = PositionComparator().pair_and_compare(live, bt)
    pair = pairs[0]

    assert pair.position_im_delta == Decimal("0")
    assert pair.position_mm_delta == Decimal("0")
    assert pair.liq_price_delta == Decimal("0")
    assert pair.unrealised_pnl_delta == Decimal("0")
    assert pair.cum_realised_pnl_delta == Decimal("0")


def test_unrealised_recomputation_divergent_entry(base_ts):
    """Backtest entry diverges from live → unrealised_pnl_delta picks it up."""
    # Long, mark=110, live entry=100 (pnl=10), bt entry=105 (pnl=5)
    live = [_snap(
        "Buy", base_ts, size=Decimal("1"),
        entry_price=Decimal("100"), mark_price=Decimal("110"),
        source="live",
    )]
    bt = [_snap(
        "Buy", base_ts, size=Decimal("1"),
        entry_price=Decimal("105"), mark_price=Decimal("110"),
        source="backtest",
    )]

    pair = PositionComparator().pair_and_compare(live, bt)[0]

    # bt minus live: 5 - 10 = -5
    assert pair.unrealised_pnl_recomputed_live == Decimal("10")
    assert pair.unrealised_pnl_recomputed_bt == Decimal("5")
    assert pair.unrealised_pnl_delta == Decimal("-5")


def test_short_side_recomputation_correct_sign(base_ts):
    """Short pair: mark < entry → positive unrealized (long-only formula gives wrong sign)."""
    # Short, entry=100, mark=90, size=1 → unrealized = (100-90)*1 = +10
    live = [_snap(
        "Sell", base_ts, size=Decimal("1"),
        entry_price=Decimal("100"), mark_price=Decimal("90"),
        source="live",
    )]
    bt = [_snap(
        "Sell", base_ts, size=Decimal("1"),
        entry_price=Decimal("100"), mark_price=Decimal("90"),
        source="backtest",
    )]

    pair = PositionComparator().pair_and_compare(live, bt)[0]

    assert pair.unrealised_pnl_recomputed_live == Decimal("10")
    assert pair.unrealised_pnl_recomputed_bt == Decimal("10")
    assert pair.unrealised_pnl_delta == Decimal("0")


def test_null_telemetry_per_field(base_ts):
    """Live row with NULL position_im → im_delta None, other deltas present."""
    live = [_snap("Buy", base_ts, source="live")]
    live[0].position_im = None  # simulate partial Bybit payload
    bt = [_snap("Buy", base_ts, source="backtest")]

    pair = PositionComparator().pair_and_compare(live, bt)[0]

    assert pair.position_im_delta is None
    assert pair.position_mm_delta == Decimal("0")
    assert pair.liq_price_delta == Decimal("0")
    assert pair.has_missing_telemetry is True

    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, [pair])
    assert metrics.position_pairs_compared == 1
    assert metrics.position_pairs_missing_telemetry == 1


def test_cum_realised_pnl_final_delta_aggregates_both_sides(base_ts):
    """Both-side: final delta sums per-side last-pair deltas.

    Without per-side aggregation, matched[-1] would always be "last Sell pair"
    and a long-only divergence would be silently masked when any Sell pair
    exists. Regression test for P2 finding in the 0034 code review.
    """
    # Long final delta = +5 (live cum=10, bt cum=15), short final = 0.
    live = [
        _snap("Buy", base_ts, cum_realised_pnl=Decimal("10"), source="live"),
        _snap("Sell", base_ts + timedelta(seconds=1), cum_realised_pnl=Decimal("3"), source="live"),
    ]
    bt = [
        _snap("Buy", base_ts, cum_realised_pnl=Decimal("15"), source="backtest"),
        _snap("Sell", base_ts + timedelta(seconds=1), cum_realised_pnl=Decimal("3"), source="backtest"),
    ]
    pairs = PositionComparator().pair_and_compare(live, bt)
    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    assert metrics.cum_realised_pnl_final_delta == Decimal("5")


def test_cum_realised_pnl_final_delta_only_short_diverges(base_ts):
    """Short-only divergence: short delta surfaces in aggregate."""
    live = [
        _snap("Buy", base_ts, cum_realised_pnl=Decimal("0"), source="live"),
        _snap("Sell", base_ts + timedelta(seconds=1), cum_realised_pnl=Decimal("0"), source="live"),
    ]
    bt = [
        _snap("Buy", base_ts, cum_realised_pnl=Decimal("0"), source="backtest"),
        _snap("Sell", base_ts + timedelta(seconds=1), cum_realised_pnl=Decimal("-2"), source="backtest"),
    ]
    pairs = PositionComparator().pair_and_compare(live, bt)
    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    assert metrics.cum_realised_pnl_final_delta == Decimal("-2")


def test_cur_realised_pnl_final_delta_aggregates_both_sides(base_ts):
    """0056: cycle-scoped final delta sums per-side last-pair deltas."""
    live = [
        _snap("Buy", base_ts, cur_realised_pnl=Decimal("4"), source="live"),
        _snap("Sell", base_ts + timedelta(seconds=1), cur_realised_pnl=Decimal("0"), source="live"),
    ]
    bt = [
        _snap("Buy", base_ts, cur_realised_pnl=Decimal("6"), source="backtest"),
        _snap("Sell", base_ts + timedelta(seconds=1), cur_realised_pnl=Decimal("-1"), source="backtest"),
    ]
    pairs = PositionComparator().pair_and_compare(live, bt)
    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    # Long delta = +2, Short delta = -1 → sum = +1.
    assert metrics.cur_realised_pnl_final_delta == Decimal("1")


def test_cur_realised_pnl_delta_null_pair_skipped_in_aggregate(base_ts):
    """Pre-0056 NULL pairs are skipped during cur_realised_pnl aggregation."""
    live = [_snap("Buy", base_ts, cur_realised_pnl=None, source="live")]  # type: ignore[arg-type]
    bt = [_snap("Buy", base_ts, cur_realised_pnl=None, source="backtest")]  # type: ignore[arg-type]
    pairs = PositionComparator().pair_and_compare(live, bt)
    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    assert metrics.cur_realised_pnl_final_delta == Decimal("0")


def test_cur_realised_pnl_null_does_not_trigger_missing_telemetry(base_ts):
    """0056: NULL cur_realised_pnl delta alone must NOT set has_missing_telemetry.

    Pre-0056 rows ship with NULL in this column; treating that as missing
    telemetry would universally trip the flag for legacy replay sessions.
    """
    live = [_snap("Buy", base_ts, cur_realised_pnl=None, source="live")]  # type: ignore[arg-type]
    bt = [_snap("Buy", base_ts, cur_realised_pnl=None, source="backtest")]  # type: ignore[arg-type]
    pairs = PositionComparator().pair_and_compare(live, bt)
    assert len(pairs) == 1
    assert pairs[0].cur_realised_pnl_delta is None
    assert pairs[0].has_missing_telemetry is False


def test_cur_realised_pnl_delta_none_on_unmatched_bt_marker(base_ts):
    """0056: unmatched-bt sentinel sets cur_realised_pnl_delta = None."""
    bt_only = base_ts
    live_far = base_ts + timedelta(seconds=PAIR_TOLERANCE_S + 30)
    live = [_snap("Buy", live_far, source="live")]
    bt = [_snap("Buy", bt_only, source="backtest")]
    pairs = PositionComparator().pair_and_compare(live, bt)
    unmatched = [p for p in pairs if p.live is None]
    assert len(unmatched) == 1
    assert unmatched[0].cur_realised_pnl_delta is None


def test_pos_value_and_upnl_usdt_deltas_aggregate(base_ts):
    """0059: per-snapshot upnl_usdt and pos_value deltas aggregate over pairs."""
    live = [
        _snap(
            "Buy", base_ts + timedelta(seconds=2),
            unrealised_pnl=Decimal("2"), position_value=Decimal("100"),
            source="live",
        )
    ]
    bt = [
        _snap(
            "Buy", base_ts,
            unrealised_pnl=Decimal("3"), position_value=Decimal("110"),
            source="backtest",
        )
    ]
    pairs = PositionComparator().pair_and_compare(live, bt)
    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    # upnl delta = 3 - 2 = 1 (stored values, NOT recomputed-vs-mark).
    assert metrics.upnl_usdt_mean_abs_delta == Decimal("1")
    assert metrics.upnl_usdt_max_abs_delta == Decimal("1")
    # pos_value delta = 110 - 100 = 10.
    assert metrics.pos_value_usdt_mean_abs_delta == Decimal("10")
    assert metrics.pos_value_usdt_max_abs_delta == Decimal("10")
    assert metrics.pos_value_final_delta == Decimal("10")


def test_cur_cum_realised_usdt_per_snapshot_aggregates(base_ts):
    """0059: cur/cum per-snapshot aggregates reuse the existing delta fields."""
    live = [
        _snap(
            "Buy", base_ts + timedelta(seconds=2),
            cur_realised_pnl=Decimal("4"), cum_realised_pnl=Decimal("10"),
            source="live",
        )
    ]
    bt = [
        _snap(
            "Buy", base_ts,
            cur_realised_pnl=Decimal("6"), cum_realised_pnl=Decimal("15"),
            source="backtest",
        )
    ]
    pairs = PositionComparator().pair_and_compare(live, bt)
    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    # cur delta = 6 - 4 = 2; cum delta = 15 - 10 = 5.
    assert metrics.cur_realised_usdt_mean_abs_delta == Decimal("2")
    assert metrics.cur_realised_usdt_max_abs_delta == Decimal("2")
    assert metrics.cum_realised_usdt_mean_abs_delta == Decimal("5")
    assert metrics.cum_realised_usdt_max_abs_delta == Decimal("5")


def test_position_value_null_pair_skipped_in_aggregate(base_ts):
    """0059: NULL position_value pair is skipped and does NOT trip telemetry flag."""
    live = [_snap("Buy", base_ts, position_value=None, source="live")]  # type: ignore[arg-type]
    bt = [_snap("Buy", base_ts, position_value=None, source="backtest")]  # type: ignore[arg-type]
    pairs = PositionComparator().pair_and_compare(live, bt)
    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    assert metrics.pos_value_usdt_mean_abs_delta == Decimal("0")
    assert metrics.pos_value_usdt_max_abs_delta == Decimal("0")
    assert metrics.pos_value_final_delta == Decimal("0")
    # NULL position_value alone must not flag the pair as missing telemetry.
    assert metrics.position_pairs_missing_telemetry == 0
    assert pairs[0].pos_value_delta is None
    assert pairs[0].has_missing_telemetry is False


def test_new_deltas_none_on_unmatched_bt_marker(base_ts):
    """0059: unmatched-bt sentinel sets upnl_usdt_delta and pos_value_delta = None."""
    bt_only = base_ts
    live_far = base_ts + timedelta(seconds=PAIR_TOLERANCE_S + 30)
    live = [_snap("Buy", live_far, source="live")]
    bt = [_snap("Buy", bt_only, source="backtest")]
    pairs = PositionComparator().pair_and_compare(live, bt)
    unmatched = [p for p in pairs if p.live is None]
    assert len(unmatched) == 1
    assert unmatched[0].upnl_usdt_delta is None
    assert unmatched[0].pos_value_delta is None


def test_pos_value_final_delta_aggregates_both_sides(base_ts):
    """0059: position-value final delta sums per-side last-pair deltas."""
    live = [
        _snap("Buy", base_ts, position_value=Decimal("100"), source="live"),
        _snap("Sell", base_ts + timedelta(seconds=1), position_value=Decimal("50"), source="live"),
    ]
    bt = [
        _snap("Buy", base_ts, position_value=Decimal("110"), source="backtest"),
        _snap("Sell", base_ts + timedelta(seconds=1), position_value=Decimal("48"), source="backtest"),
    ]
    pairs = PositionComparator().pair_and_compare(live, bt)
    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    # Long delta = +10, Short delta = -2 → sum = +8.
    assert metrics.pos_value_final_delta == Decimal("8")


def test_fold_metrics_idempotent(base_ts):
    """Calling fold_metrics_into twice yields the same final values."""
    live = [_snap("Buy", base_ts, source="live")]
    bt = [_snap("Buy", base_ts, source="backtest", position_im=Decimal("11"))]

    pairs = PositionComparator().pair_and_compare(live, bt)
    metrics = ValidationMetrics()
    PositionComparator().fold_metrics_into(metrics, pairs)
    snapshot1 = metrics.position_im_max_abs_delta

    PositionComparator().fold_metrics_into(metrics, pairs)
    snapshot2 = metrics.position_im_max_abs_delta
    assert snapshot1 == snapshot2


def test_un_migrated_db_raises():
    """Load_position_snapshots raises on missing source column.

    Simulates an un-migrated DB by creating a position_snapshots table
    without the 0034 columns and the source-aware index.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE position_snapshots ("
            "  id INTEGER PRIMARY KEY,"
            "  run_id TEXT,"
            "  account_id TEXT NOT NULL,"
            "  symbol TEXT NOT NULL,"
            "  exchange_ts TIMESTAMP NOT NULL,"
            "  local_ts TIMESTAMP NOT NULL,"
            "  side TEXT NOT NULL,"
            "  size NUMERIC NOT NULL,"
            "  entry_price NUMERIC NOT NULL,"
            "  liq_price NUMERIC,"
            "  unrealised_pnl NUMERIC,"
            "  raw_json TEXT"
            ")"
        ))

    Session = sessionmaker(bind=engine)
    with Session() as session:
        with pytest.raises(PositionTelemetryNotMigratedError):
            load_position_snapshots(
                session, run_id="r", symbol="LTCUSDT", source="live",
            )


def test_un_migrated_0056_raises():
    """0056: 0034 columns present but cur_realised_pnl missing → raise.

    Mirrors the 0034 un-migrated test but constructs a DB that already
    has the 0034 columns. The probe should reach the 0056 check and
    raise with the 0056 migration message.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE position_snapshots ("
            "  id INTEGER PRIMARY KEY,"
            "  run_id TEXT,"
            "  account_id TEXT NOT NULL,"
            "  symbol TEXT NOT NULL,"
            "  exchange_ts TIMESTAMP NOT NULL,"
            "  local_ts TIMESTAMP NOT NULL,"
            "  side TEXT NOT NULL,"
            "  size NUMERIC NOT NULL,"
            "  entry_price NUMERIC NOT NULL,"
            "  liq_price NUMERIC,"
            "  unrealised_pnl NUMERIC,"
            "  source TEXT NOT NULL DEFAULT 'live',"
            "  mark_price NUMERIC,"
            "  position_im NUMERIC,"
            "  position_mm NUMERIC,"
            "  cum_realised_pnl NUMERIC,"
            "  raw_json TEXT"
            ")"
        ))

    Session = sessionmaker(bind=engine)
    with Session() as session:
        with pytest.raises(
            PositionTelemetryNotMigratedError, match="0056",
        ):
            load_position_snapshots(
                session, run_id="r", symbol="LTCUSDT", source="live",
            )


def test_un_migrated_0059_raises():
    """0059: 0056 columns present but position_value missing → raise.

    Mirrors the 0056 un-migrated test but constructs a DB that already
    has the 0034 and 0056 columns. The probe should reach the 0059 check
    and raise with the 0059 migration message.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE position_snapshots ("
            "  id INTEGER PRIMARY KEY,"
            "  run_id TEXT,"
            "  account_id TEXT NOT NULL,"
            "  symbol TEXT NOT NULL,"
            "  exchange_ts TIMESTAMP NOT NULL,"
            "  local_ts TIMESTAMP NOT NULL,"
            "  side TEXT NOT NULL,"
            "  size NUMERIC NOT NULL,"
            "  entry_price NUMERIC NOT NULL,"
            "  liq_price NUMERIC,"
            "  unrealised_pnl NUMERIC,"
            "  source TEXT NOT NULL DEFAULT 'live',"
            "  mark_price NUMERIC,"
            "  position_im NUMERIC,"
            "  position_mm NUMERIC,"
            "  cum_realised_pnl NUMERIC,"
            "  cur_realised_pnl NUMERIC,"
            "  raw_json TEXT"
            ")"
        ))

    Session = sessionmaker(bind=engine)
    with Session() as session:
        with pytest.raises(
            PositionTelemetryNotMigratedError, match="0059",
        ):
            load_position_snapshots(
                session, run_id="r", symbol="LTCUSDT", source="live",
            )


# ---------------------------------------------------------------------------
# 0044: state-consistency filter
# ---------------------------------------------------------------------------


class TestStateConsistencyFilter:
    """Pairs matched by exchange_ts but with drifted position state are
    flagged ``state_diverged`` and excluded from delta aggregates.
    """

    def test_consistent_pair_not_diverged(self, base_ts):
        """Matching size + entry within tolerance → state_diverged=False."""
        live = [_snap("Buy", base_ts, size=Decimal("1.0"), entry_price=Decimal("100"), source="live")]
        bt = [_snap("Buy", base_ts, size=Decimal("1.0"), entry_price=Decimal("100"), source="backtest")]
        pairs = PositionComparator().pair_and_compare(live, bt)
        assert len(pairs) == 1
        assert pairs[0].state_diverged is False
        metrics = ValidationMetrics()
        PositionComparator().fold_metrics_into(metrics, pairs)
        assert metrics.position_pairs_compared == 1
        assert metrics.position_pairs_state_diverged == 0

    def test_size_diverged_pair_flagged(self, base_ts):
        """|live.size - bt.size| > tol → state_diverged=True, excluded."""
        live = [_snap("Buy", base_ts, size=Decimal("4.8"), entry_price=Decimal("57.45"),
                      liq_price=Decimal("14.5"), source="live")]
        bt = [_snap("Buy", base_ts, size=Decimal("3.1"), entry_price=Decimal("57.45"),
                    liq_price=Decimal("0"), source="backtest")]
        pairs = PositionComparator().pair_and_compare(live, bt)
        assert pairs[0].state_diverged is True

        metrics = ValidationMetrics()
        PositionComparator().fold_metrics_into(metrics, pairs)
        # The diverged pair is COUNTED only in the diverged bucket.
        assert metrics.position_pairs_state_diverged == 1
        assert metrics.position_pairs_compared == 0
        # The 14.5 USDT liq_delta does NOT pollute the headline metric.
        assert metrics.liq_price_max_abs_delta == Decimal("0")
        assert metrics.liq_price_mean_abs_delta == Decimal("0")

    def test_entry_diverged_pair_flagged(self, base_ts):
        """|live.entry - bt.entry|/live.entry > tol → state_diverged=True."""
        live = [_snap("Buy", base_ts, size=Decimal("1.0"), entry_price=Decimal("100"), source="live")]
        bt = [_snap("Buy", base_ts, size=Decimal("1.0"), entry_price=Decimal("101"), source="backtest")]
        # |101 - 100| / 100 = 0.01 = 1% > 0.1% tolerance
        pairs = PositionComparator().pair_and_compare(live, bt)
        assert pairs[0].state_diverged is True

    def test_size_within_tolerance_inclusive(self, base_ts):
        """|Δsize| == size_tolerance → still counted (boundary inclusive)."""
        live = [_snap("Buy", base_ts, size=Decimal("1.000"), source="live")]
        bt = [_snap("Buy", base_ts, size=Decimal("1.001"), source="backtest")]
        # Exactly equal to default size_tol (0.001).
        pairs = PositionComparator().pair_and_compare(live, bt)
        assert pairs[0].state_diverged is False

    def test_both_zero_size_not_diverged(self, base_ts):
        """Closed-on-both-sides matches trivially without divergence flag."""
        live = [_snap("Buy", base_ts, size=Decimal("0"), entry_price=Decimal("0"), source="live")]
        bt = [_snap("Buy", base_ts, size=Decimal("0"), entry_price=Decimal("0"), source="backtest")]
        pairs = PositionComparator().pair_and_compare(live, bt)
        assert pairs[0].state_diverged is False

    def test_one_zero_one_nonzero_flagged(self, base_ts):
        """Asymmetric closure: one side empty, other not → diverged."""
        live = [_snap("Buy", base_ts, size=Decimal("0.5"), entry_price=Decimal("100"), source="live")]
        bt = [_snap("Buy", base_ts, size=Decimal("0"), entry_price=Decimal("0"), source="backtest")]
        pairs = PositionComparator().pair_and_compare(live, bt)
        assert pairs[0].state_diverged is True

    def test_custom_tolerances_relaxed(self, base_ts):
        """Operator may relax tolerances; diverged pairs in default become consistent."""
        live = [_snap("Buy", base_ts, size=Decimal("4.8"), entry_price=Decimal("57.45"), source="live")]
        bt = [_snap("Buy", base_ts, size=Decimal("3.1"), entry_price=Decimal("57.45"), source="backtest")]
        comp = PositionComparator(
            state_size_tolerance=Decimal("2.0"),
            state_entry_rel_tolerance=Decimal("0.1"),
        )
        pairs = comp.pair_and_compare(live, bt)
        # |1.7| < 2.0 → considered consistent under relaxed thresholds.
        assert pairs[0].state_diverged is False
