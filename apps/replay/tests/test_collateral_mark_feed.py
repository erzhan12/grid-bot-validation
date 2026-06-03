"""Unit tests for CollateralMarkFeed (feature 0065).

Carry-forward, at-or-before, monotonic-cursor mark lookup over
``ticker_snapshots`` for non-USDT collateral coins.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from grid_db import TickerSnapshot, TickerSnapshotRepository

from replay.engine import CollateralMarkFeed


BASE = datetime(2026, 6, 1, 17, 0, 0, tzinfo=timezone.utc)


def _ticker(symbol, ts, mark):
    return TickerSnapshot(
        symbol=symbol, exchange_ts=ts, local_ts=ts,
        last_price=mark, mark_price=mark,
        bid1_price=mark, ask1_price=mark, funding_rate=Decimal("0.0001"),
    )


@pytest.fixture
def feed_db(db):
    with db.get_session() as session:
        TickerSnapshotRepository(session).bulk_insert([
            _ticker("SOLUSDT", BASE, Decimal("80")),
            _ticker("SOLUSDT", BASE + timedelta(seconds=30), Decimal("90")),
            _ticker("SOLUSDT", BASE + timedelta(seconds=60), Decimal("85")),
        ])
        session.commit()
    return db


class TestCollateralMarkFeed:
    def test_carry_forward_at_or_before(self, feed_db):
        feed = CollateralMarkFeed(
            db=feed_db, symbol_for={"SOL": "SOLUSDT"},
            start_ts=BASE, end_ts=BASE + timedelta(minutes=5),
        )
        # Between t0 and t0+30 → carry t0 mark.
        assert feed.mark_at("SOL", BASE + timedelta(seconds=10)) == Decimal("80")
        # At/after t0+30 → newer.
        assert feed.mark_at("SOL", BASE + timedelta(seconds=45)) == Decimal("90")
        # At/after t0+60 → newest.
        assert feed.mark_at("SOL", BASE + timedelta(seconds=90)) == Decimal("85")

    def test_none_before_first_row(self, feed_db):
        feed = CollateralMarkFeed(
            db=feed_db, symbol_for={"SOL": "SOLUSDT"},
            start_ts=BASE - timedelta(minutes=1), end_ts=BASE + timedelta(minutes=5),
        )
        assert feed.mark_at("SOL", BASE - timedelta(seconds=1)) is None
        # Then advances normally.
        assert feed.mark_at("SOL", BASE + timedelta(seconds=1)) == Decimal("80")

    def test_unknown_coin_returns_none(self, feed_db):
        feed = CollateralMarkFeed(
            db=feed_db, symbol_for={"SOL": "SOLUSDT"},
            start_ts=BASE, end_ts=BASE + timedelta(minutes=5),
        )
        assert feed.mark_at("DOGE", BASE + timedelta(seconds=10)) is None

    def test_exact_boundary_is_inclusive(self, feed_db):
        feed = CollateralMarkFeed(
            db=feed_db, symbol_for={"SOL": "SOLUSDT"},
            start_ts=BASE, end_ts=BASE + timedelta(minutes=5),
        )
        # exactly at t0+30 → that row (<=, inclusive).
        assert feed.mark_at("SOL", BASE + timedelta(seconds=30)) == Decimal("90")

    def test_non_monotonic_ts_raises(self, feed_db):
        """Forward-only cursor cannot rewind → reject decreasing ts loudly."""
        feed = CollateralMarkFeed(
            db=feed_db, symbol_for={"SOL": "SOLUSDT"},
            start_ts=BASE, end_ts=BASE + timedelta(minutes=5),
        )
        feed.mark_at("SOL", BASE + timedelta(seconds=45))
        with pytest.raises(ValueError, match="non-monotonic"):
            feed.mark_at("SOL", BASE + timedelta(seconds=10))

    def test_anchors_carry_forward_mark_within_seed_window(self, db):
        """F1: a mark in [seed_at_ts, start_ts] (no row exactly at start_ts) must
        carry into the first tick (seed.at_ts < start_ts, sparse ticks)."""
        with db.get_session() as session:
            TickerSnapshotRepository(session).bulk_insert([
                _ticker("SOLUSDT", BASE - timedelta(seconds=30), Decimal("70")),
                _ticker("SOLUSDT", BASE + timedelta(seconds=30), Decimal("90")),
            ])
            session.commit()
        feed = CollateralMarkFeed(
            db=db, symbol_for={"SOL": "SOLUSDT"},
            seed_at_ts=BASE - timedelta(minutes=1),  # at_ts < start_ts
            start_ts=BASE, end_ts=BASE + timedelta(minutes=5),
        )
        # First tick at start_ts: carry-forward the in-window pre-start mark (70).
        assert feed.mark_at("SOL", BASE) == Decimal("70")
        # Then advances to the in-window mark.
        assert feed.mark_at("SOL", BASE + timedelta(seconds=60)) == Decimal("90")

    def test_pre_seed_mark_not_anchored(self, db):
        """P1 (PR #158): a ticker mark BEFORE seed_at_ts must NOT anchor the
        carry-forward when start_ts > seed.at_ts (else false backward drift)."""
        with db.get_session() as session:
            TickerSnapshotRepository(session).bulk_insert([
                _ticker("SOLUSDT", BASE - timedelta(seconds=30), Decimal("70")),  # pre-seed
                _ticker("SOLUSDT", BASE + timedelta(minutes=2), Decimal("95")),   # in-window
            ])
            session.commit()
        feed = CollateralMarkFeed(
            db=db, symbol_for={"SOL": "SOLUSDT"},
            seed_at_ts=BASE, start_ts=BASE + timedelta(minutes=1),
            end_ts=BASE + timedelta(minutes=10),
        )
        # No mark in [seed_at_ts, start_ts] → anchor None → keep seed mark.
        assert feed.mark_at("SOL", BASE + timedelta(minutes=1)) is None
        # A genuine in-window mark is still picked up.
        assert feed.mark_at("SOL", BASE + timedelta(minutes=3)) == Decimal("95")

    def test_multi_batch_pagination_holds_session(self, feed_db):
        """batch_size=1 forces the cursor to re-query across MULTIPLE batches on
        the session held open inside the generator (the `with`-in-generator does
        NOT close at a yield). Proves carry-forward survives batch boundaries."""
        feed = CollateralMarkFeed(
            db=feed_db, symbol_for={"SOL": "SOLUSDT"},
            start_ts=BASE, end_ts=BASE + timedelta(minutes=5),
            batch_size=1,  # 3 rows → 3 separate batch fetches on one session
        )
        assert feed.mark_at("SOL", BASE + timedelta(seconds=10)) == Decimal("80")
        assert feed.mark_at("SOL", BASE + timedelta(seconds=45)) == Decimal("90")
        assert feed.mark_at("SOL", BASE + timedelta(seconds=90)) == Decimal("85")
