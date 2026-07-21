"""Tests for shared-wallet reconciliation helpers."""

from datetime import timedelta, timezone
from decimal import Decimal

from grid_db import WalletSnapshot
from grid_db.models import Run

from live_check.shared_wallet import (
    WalletCurvePoint,
    load_wallet_curve,
    reconcile_wallet_curve,
)
from live_check.window import Window
from replay.multi_engine import AccountCurveSample


RUN_ID = "test-run-id"


def _insert_wallet(
    db,
    account_id,
    *,
    run_id=RUN_ID,
    ts,
    equity=None,
    margin=None,
    mm_rate=None,
    coin="USDT",
):
    with db.get_session() as session:
        session.add(
            WalletSnapshot(
                run_id=run_id,
                account_id=account_id,
                exchange_ts=ts,
                local_ts=ts,
                coin=coin,
                wallet_balance=Decimal("100"),
                available_balance=Decimal("90"),
                total_available_balance=Decimal("90"),
                total_equity=Decimal(equity) if equity is not None else None,
                total_margin_balance=(
                    Decimal(margin) if margin is not None else None
                ),
                account_mm_rate=Decimal(mm_rate) if mm_rate is not None else None,
            )
        )


def test_load_wallet_curve_run_filters_and_dedups_end_anchor(
    db, seeded_run_account, ts
):
    """C6: range read is post-filtered by run_id and end anchor is de-duped."""
    acc = seeded_run_account.account_id
    # A second recording run on the SAME account — its wallet rows must be
    # excluded by the run_id post-filter (C6). The FK to runs requires the
    # run to exist, so mirror the seeded run's user/account/strategy refs.
    with db.get_session() as session:
        seeded = session.get(Run, RUN_ID)
        session.add(
            Run(
                run_id="other-run",
                user_id=seeded.user_id,
                account_id=seeded.account_id,
                strategy_id=seeded.strategy_id,
                run_type="recording",
                start_ts=seeded.start_ts,
            )
        )
        session.commit()
    _insert_wallet(db, acc, ts=ts, equity="100", margin="100", mm_rate="0.01")
    _insert_wallet(
        db,
        acc,
        run_id="other-run",
        ts=ts + timedelta(minutes=1),
        equity="999",
        margin="999",
        mm_rate="0.99",
    )
    _insert_wallet(
        db,
        acc,
        ts=ts + timedelta(minutes=2),
        equity="101",
        margin="101",
        mm_rate="0.02",
    )
    window = Window(start=ts, end=ts + timedelta(minutes=2))
    with db.get_readonly_session() as session:
        rows = load_wallet_curve(session, RUN_ID, acc, "USDT", window)
    assert [row.exchange_ts for row in rows] == [
        ts,
        ts + timedelta(minutes=2),
    ]
    assert [row.total_equity for row in rows] == [
        Decimal("100.00000000"),
        Decimal("101.00000000"),
    ]


def test_reconcile_wallet_curve_skips_nulls_per_field(ts):
    """C7: NULL recorded fields are skipped independently, never zero-filled."""
    recorded = [
        WalletCurvePoint(
            exchange_ts=ts,
            total_equity=Decimal("100"),
            total_margin_balance=None,
            account_mm_rate=Decimal("0.01"),
        ),
        WalletCurvePoint(
            exchange_ts=ts + timedelta(seconds=1),
            total_equity=None,
            total_margin_balance=Decimal("101"),
            account_mm_rate=None,
        ),
    ]
    replay = [
        AccountCurveSample(
            exchange_ts=ts,
            total_equity=Decimal("100.2"),
            total_margin_balance=Decimal("100.2"),
            account_mm_rate=Decimal("0.011"),
        ),
        AccountCurveSample(
            exchange_ts=ts + timedelta(seconds=1),
            total_equity=Decimal("100.3"),
            total_margin_balance=Decimal("100.8"),
            account_mm_rate=Decimal("0.012"),
        ),
    ]
    diff = reconcile_wallet_curve(replay, recorded)
    assert diff.max_equity_delta == Decimal("0.2")
    assert diff.equity_points == 1
    assert diff.max_margin_balance_delta == Decimal("0.2")
    assert diff.margin_balance_points == 1
    assert diff.max_account_mm_rate_delta == Decimal("0.001")
    assert diff.account_mm_rate_points == 1


def test_reconcile_wallet_curve_mixed_tz_awareness(ts):
    """Review P2: mixed tz-aware/naive timestamps normalize to naive UTC and
    do not raise a TypeError mid-walk (recorded aware, replay naive here)."""
    aware = ts.replace(tzinfo=timezone.utc)
    recorded = [
        WalletCurvePoint(
            exchange_ts=aware,
            total_equity=Decimal("100"),
            total_margin_balance=None,
            account_mm_rate=None,
        )
    ]
    replay = [
        AccountCurveSample(
            exchange_ts=ts,  # naive — opposite awareness from recorded
            total_equity=Decimal("100.4"),
            total_margin_balance=Decimal("100.4"),
            account_mm_rate=Decimal("0.01"),
        )
    ]
    diff = reconcile_wallet_curve(replay, recorded)
    assert diff.equity_points == 1
    assert diff.max_equity_delta == Decimal("0.4")
