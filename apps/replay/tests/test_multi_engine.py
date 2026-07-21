"""Tests for shared-wallet multi replay orchestration helpers."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gridcore import EventType, TickerEvent
from grid_db.models import PrivateExecution

from backtest.data_provider import InMemoryDataProvider
from backtest.fill_simulator import FillMode
from backtest.session import BacktestSession, BacktestTrade

from replay.multi_config import MultiReplayConfig
from replay.multi_engine import (
    MultiReplayEngine,
    _SharedSessionCoordinator,
)


TS = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _tick(symbol: str, price: str, offset_ms: int) -> TickerEvent:
    ts = TS + timedelta(milliseconds=offset_ms)
    px = Decimal(price)
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol=symbol,
        exchange_ts=ts,
        local_ts=ts,
        last_price=px,
        mark_price=px,
        bid1_price=px - Decimal("0.1"),
        ask1_price=px + Decimal("0.1"),
        funding_rate=Decimal("0"),
    )


@dataclass
class _Tracker:
    unrealized: Decimal

    @property
    def state(self):
        return SimpleNamespace(size=Decimal("1"), avg_entry_price=Decimal("1"))

    def calculate_unrealized_pnl(self, price: Decimal) -> Decimal:
        return self.unrealized + (price * Decimal("0"))


class _Runner:
    def __init__(self, long_upnl: str, short_upnl: str, mm: str = "0"):
        self.long_tracker = _Tracker(Decimal(long_upnl))
        self.short_tracker = _Tracker(Decimal(short_upnl))
        self._mm = Decimal(mm)

    def _estimate_pair_im_mm(self, _long, _short, _price):
        return Decimal("1"), self._mm, Decimal("2"), self._mm


def _trade(symbol: str, pnl: str, fee: str) -> BacktestTrade:
    return BacktestTrade(
        trade_id=f"{symbol}-{pnl}",
        symbol=symbol,
        side="Buy",
        price=Decimal("10"),
        qty=Decimal("1"),
        direction="long",
        timestamp=TS,
        order_id=f"oid-{symbol}",
        client_order_id=f"cid-{symbol}",
        realized_pnl=Decimal(pnl),
        commission=Decimal(fee),
        strat_id=f"{symbol.lower()}_test",
    )


class TestTickMerge:
    def test_k_way_merge_ascending_and_symbol_scoped(self):
        """Merged stream is globally ascending with deterministic equal-ts ties."""
        providers = {
            "SOLUSDT": InMemoryDataProvider([
                _tick("SOLUSDT", "100", 0),
                _tick("SOLUSDT", "101", 2),
            ]),
            "LTCUSDT": InMemoryDataProvider([
                _tick("LTCUSDT", "80", 1),
                _tick("LTCUSDT", "81", 2),
            ]),
        }
        merged = list(MultiReplayEngine._merge_ticks(providers))
        assert [(symbol, tick.exchange_ts) for symbol, tick in merged] == [
            ("SOLUSDT", TS),
            ("LTCUSDT", TS + timedelta(milliseconds=1)),
            ("LTCUSDT", TS + timedelta(milliseconds=2)),
            ("SOLUSDT", TS + timedelta(milliseconds=2)),
        ]
        assert all(symbol == tick.symbol for symbol, tick in merged)


class TestSharedSessionCoordinator:
    def test_merged_pending_sums_across_runners(self):
        """C1: concurrent pending rollups sum instead of last-writer clobber."""
        session = BacktestSession(initial_balance=Decimal("100"))
        runners = {"SOLUSDT": _Runner("0", "0"), "LTCUSDT": _Runner("0", "0")}
        coord = _SharedSessionCoordinator(
            session,
            runners,
            {"SOLUSDT": Decimal("100"), "LTCUSDT": Decimal("80")},
        )
        with coord.active("SOLUSDT"):
            session.set_pending_wallet(Decimal("1.5"), Decimal("0.1"))
        with coord.active("LTCUSDT"):
            session.set_pending_wallet(Decimal("-0.5"), Decimal("0.2"))
        assert session._pending_realized_pnl == Decimal("1.0")
        assert session._pending_commission == Decimal("0.3")

    def test_refresh_balances_intercepts_sum_unrealized(self):
        """C2: in-method refresh/read sees account-wide unrealized."""
        session = BacktestSession(initial_balance=Decimal("100"))
        runners = {
            "SOLUSDT": _Runner("5", "0"),
            "LTCUSDT": _Runner("-2", "0"),
        }
        coord = _SharedSessionCoordinator(
            session,
            runners,
            {"SOLUSDT": Decimal("100"), "LTCUSDT": Decimal("80")},
        )
        seen_wallet = None
        with coord.active("SOLUSDT"):
            session.refresh_balances(Decimal("5"))
            seen_wallet = session.current_balance
        assert seen_wallet == Decimal("103")
        assert session.total_equity == Decimal("103")

    def test_account_margin_series_units(self):
        """C3/C4: emitted sample has equity, margin balance and ratio units."""
        session = BacktestSession(initial_balance=Decimal("100"))
        session.update_equity(TS, Decimal("10"), Decimal("4"), Decimal("2"))
        sample = MultiReplayEngine._account_sample(TS, session, Decimal("2"))
        assert sample.total_equity == Decimal("110")
        assert sample.total_margin_balance == Decimal("110")
        assert sample.account_mm_rate == Decimal("0.01818181818181818181818181818")

    def test_finalize_uses_sum_unrealized(self):
        """Final metrics retain both books' open unrealized."""
        session = BacktestSession(initial_balance=Decimal("100"))
        session.record_trade(_trade("SOLUSDT", "1", "0.1"))
        metrics = session.finalize(Decimal("7"))
        assert metrics.total_unrealized_pnl == Decimal("7")
        assert metrics.net_pnl == Decimal("7.9")


class TestSharedWalletCoupling:
    def test_record_trade_accumulates_shared_wallet(self):
        """One shared session accumulates realized PnL and fees across symbols."""
        session = BacktestSession(initial_balance=Decimal("100"))
        session.record_trade(_trade("SOLUSDT", "-5", "0.1"))
        session.record_trade(_trade("LTCUSDT", "2", "0.2"))
        session.refresh_balances(Decimal("0"))
        assert session.current_balance == Decimal("96.7")

    def test_o2_startup_mark_cache_from_in_memory_provider(self):
        """O2 startup cache seeds idle-symbol marks before the first merge tick."""
        config = MultiReplayConfig(
            start_ts=TS,
            end_ts=TS + timedelta(seconds=1),
            strategies=[
                {"symbol": "SOLUSDT", "strat_id": "sol", "tick_size": "0.01"},
                {"symbol": "LTCUSDT", "strat_id": "ltc", "tick_size": "0.01"},
            ],
        )
        engine = MultiReplayEngine.__new__(MultiReplayEngine)
        providers = {
            "SOLUSDT": InMemoryDataProvider([_tick("SOLUSDT", "100", 0)]),
            "LTCUSDT": InMemoryDataProvider([_tick("LTCUSDT", "80", 0)]),
        }
        cache = engine._startup_mark_cache(config, TS, providers)
        assert cache == {"SOLUSDT": Decimal("100"), "LTCUSDT": Decimal("80")}


class TestEventFollowerLoading:
    def test_event_follower_is_per_symbol(self, db, seeded_run_account):
        """Synthetic RecordedExecution stream is scoped to one strategy symbol."""
        with db.get_session() as session:
            for symbol in ("SOLUSDT", "LTCUSDT"):
                session.add(
                    PrivateExecution(
                        run_id="test-run-id",
                        account_id=seeded_run_account.account_id,
                        symbol=symbol,
                        exec_id=f"{symbol}-exec",
                        order_id=f"{symbol}-oid",
                        order_link_id=f"{symbol}-link",
                        exchange_ts=TS,
                        side="Buy",
                        exec_price=Decimal("10"),
                        exec_qty=Decimal("1"),
                        exec_fee=Decimal("0.01"),
                        closed_pnl=Decimal("0"),
                    )
                )
        engine = MultiReplayEngine.__new__(MultiReplayEngine)
        engine._db = db
        follower = engine._event_follower(
            "test-run-id",
            "SOLUSDT",
            TS - timedelta(seconds=1),
            TS + timedelta(seconds=1),
            FillMode.EVENT_FOLLOWER,
        )
        rows = follower.drain(TS - timedelta(seconds=1), TS)
        assert [row.exec_id for row in rows] == ["SOLUSDT-exec"]
        assert rows[0].order_id == "SOLUSDT-oid"


class TestMultiReplayRunEndToEnd:
    """End-to-end MultiReplayEngine.run() over two in-memory providers —
    exercises the wired merged-tick loop (merge → coordinator.active around
    process_fills/execute_tick → Σ update_equity → account_curve append →
    per-strat finalize), not just isolated helpers."""

    @patch("backtest.instrument_info.InstrumentInfoProvider")
    def test_run_emits_account_curve_and_shares_one_session(
        self, mock_provider_cls, db, seeded_run_account
    ):
        """C3/coupling: one shared session drives BOTH symbols; the loop emits
        an account_curve (total_equity/margin/mm-rate series) that is a
        SEPARATE series from the available-baseline session.equity_curve."""
        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.001")
        mock_info.tick_size = Decimal("0.01")
        mock_info.round_qty = lambda q: max(
            Decimal("0.001"), q.quantize(Decimal("0.001"))
        )
        mock_provider_cls.return_value.get.return_value = mock_info

        config = MultiReplayConfig(
            run_id="test-run-id",
            start_ts=TS,
            end_ts=TS + timedelta(seconds=1),
            initial_balance=Decimal("1000"),
            enable_funding=False,
            fill_simulator={"mode": "last_cross"},
            strategies=[
                {"symbol": "SOLUSDT", "strat_id": "solusdt_test",
                 "tick_size": "0.01", "grid_count": 10, "grid_step": 0.5},
                {"symbol": "LTCUSDT", "strat_id": "ltcusdt_test",
                 "tick_size": "0.01", "grid_count": 10, "grid_step": 0.5},
            ],
        )
        providers = {
            "SOLUSDT": InMemoryDataProvider(
                [_tick("SOLUSDT", "100", 0), _tick("SOLUSDT", "101", 200)]
            ),
            "LTCUSDT": InMemoryDataProvider(
                [_tick("LTCUSDT", "80", 100), _tick("LTCUSDT", "81", 300)]
            ),
        }
        engine = MultiReplayEngine(config=config, db=db)
        result = engine.run(data_providers=providers)

        # Both symbols ran against ONE shared session.
        assert set(result.strategies) == {"SOLUSDT", "LTCUSDT"}
        assert result.session is not None
        # C3/C4: one account sample per merged tick (2 + 2), three series.
        assert len(result.account_curve) == 4
        assert len(result.total_equity_curve) == 4
        assert len(result.total_margin_balance_curve) == 4
        assert len(result.account_mm_rate_curve) == 4
        # The emitted total_equity curve is a SEPARATE object/series from the
        # session's available-baseline equity_curve (C3 — distinct formula).
        assert result.total_equity_curve is not result.session.equity_curve
        # Samples are ascending by the merged timeline.
        stamps = [ts for ts, _ in result.total_equity_curve]
        assert stamps == sorted(stamps)
