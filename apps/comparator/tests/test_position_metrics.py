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
        unrealised_pnl=Decimal("0"),
        source=source,
        mark_price=mark_price,
        position_im=position_im,
        position_mm=position_mm,
        cum_realised_pnl=cum_realised_pnl,
    )


def test_pair_within_tolerance(base_ts):
    """Backtest at T pairs with first live at >= T within 5s."""
    live = [_snap("Buy", base_ts + timedelta(seconds=2), source="live")]
    bt = [_snap("Buy", base_ts, source="backtest")]

    pairs = PositionComparator().pair_and_compare(live, bt)

    assert len(pairs) == 1
    assert pairs[0].live is live[0]
    assert pairs[0].backtest is bt[0]


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
