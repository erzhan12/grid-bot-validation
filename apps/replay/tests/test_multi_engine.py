"""Tests for shared-wallet multi replay orchestration helpers."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gridcore import EventType, GridEngine, TickerEvent
from grid_db.models import PrivateExecution

from backtest.data_provider import InMemoryDataProvider
from backtest.executor import BacktestExecutor
from backtest.config import WindDownMode
from backtest.fill_simulator import FillMode
from backtest.session import BacktestSession, BacktestTrade

from replay.multi_config import MultiReplayConfig
from replay.multi_engine import (
    MultiReplayEngine,
    _SharedSessionCoordinator,
)
from replay.snapshot_loader import (
    ActiveOrderSeed,
    GridStateSeed,
    PositionStateSeed,
    WalletSeed,
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
    size: Decimal = Decimal("1")
    avg_entry_price: Decimal = Decimal("1")

    @property
    def state(self):
        return SimpleNamespace(size=self.size, avg_entry_price=self.avg_entry_price)

    def calculate_unrealized_pnl(self, price: Decimal) -> Decimal:
        return self.unrealized + (price * Decimal("0"))


class _Runner:
    def __init__(
        self,
        long_upnl: str,
        short_upnl: str,
        mm: str = "0",
        *,
        long_size: str = "1",
        short_size: str = "1",
        long_entry: str = "1",
        short_entry: str = "1",
    ):
        self.long_tracker = _Tracker(
            Decimal(long_upnl), Decimal(long_size), Decimal(long_entry)
        )
        self.short_tracker = _Tracker(
            Decimal(short_upnl), Decimal(short_size), Decimal(short_entry)
        )
        self._mm = Decimal(mm)
        self.engine = MagicMock()

    def _estimate_pair_im_mm(self, _long, _short, _price):
        return Decimal("1"), self._mm, Decimal("2"), self._mm


@dataclass
class _LoopTracker:
    size: Decimal
    avg_entry_price: Decimal
    pnl_sign: Decimal = Decimal("1")

    @property
    def state(self):
        return SimpleNamespace(size=self.size, avg_entry_price=self.avg_entry_price)

    def calculate_unrealized_pnl(self, price: Decimal) -> Decimal:
        return (price - self.avg_entry_price) * self.size * self.pnl_sign


class _LoopRunner:
    def __init__(self, calls: list[str], fill_size: Decimal | None = None):
        self.long_tracker = _LoopTracker(Decimal("1"), Decimal("100"))
        self.short_tracker = _LoopTracker(Decimal("0"), Decimal("0"), Decimal("-1"))
        self.engine = MagicMock()
        self.engine.halt_new_opens.side_effect = lambda: calls.append("halt")
        self._calls = calls
        self._fill_size = fill_size

    def _estimate_pair_im_mm(self, long, _short, _price):
        mm = long.size * Decimal("50")
        return Decimal("0"), mm, Decimal("0"), Decimal("0")

    def process_fills(self, _tick):
        self._calls.append("process_fills")
        if self._fill_size is not None:
            self.long_tracker.size = self._fill_size

    def execute_tick(self, _tick):
        self._calls.append("execute_tick")

    def finalize_event_follower(self):
        return None


def _loop_engine(session: BacktestSession, runner: _LoopRunner) -> MultiReplayEngine:
    """Build a hermetic merged-loop harness with one controllable runner."""
    strategy = SimpleNamespace(symbol="SOLUSDT", tick_size=Decimal("0.01"))
    config = SimpleNamespace(
        strategies=[strategy],
        fill_simulator=SimpleNamespace(mode="last_cross"),
        enable_funding=False,
        funding_rate=Decimal("0"),
        wind_down_mode=WindDownMode.LEAVE_OPEN,
    )
    engine = MultiReplayEngine.__new__(MultiReplayEngine)
    engine._multi_config = config
    engine._emit_backtest_snapshots = False
    engine._instrument_provider = MagicMock()
    engine._resolve_run_multi = MagicMock(return_value=("run", None, TS, TS))
    engine._load_multi_seed = MagicMock(
        return_value=(None, {"SOLUSDT": (None, None, None, [])})
    )
    engine._build_shared_session = MagicMock(return_value=session)
    engine._startup_mark_cache = MagicMock(return_value={"SOLUSDT": Decimal("100")})
    engine._strategy_config = MagicMock(return_value=SimpleNamespace())
    engine._event_follower = MagicMock(return_value=None)
    engine._init_runner = MagicMock(return_value=runner)
    engine._collateral_feed = MagicMock(return_value=None)
    engine._warn_unmarked_collateral = MagicMock()
    engine._runner_unrealized = MagicMock(return_value=Decimal("0"))
    engine._compare_symbol = MagicMock(return_value=MagicMock())
    return engine


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

    def test_in_method_place_reads_account_wide_wallet_balance(self):
        """C2 (review): the wallet_balance an in-method execute_place consumes
        during execute_tick reflects the account-wide Σ unrealized (idle book
        folded in), NOT the own-symbol value — the exact anti-pattern a
        trailing refresh would leave stale."""
        session = BacktestSession(initial_balance=Decimal("100"))
        runners = {
            "SOLUSDT": _Runner("5", "0"),   # active book: +5 own unrealized
            "LTCUSDT": _Runner("-2", "0"),  # idle book: -2, must be folded in
        }
        coord = _SharedSessionCoordinator(
            session,
            runners,
            {"SOLUSDT": Decimal("100"), "LTCUSDT": Decimal("80")},
        )
        captured = {}

        def fake_execute_place():
            # Mirrors runner.execute_place(wallet_balance=session.current_balance)
            captured["wallet_balance"] = session.current_balance

        # Simulate execute_tick internals: own-symbol refresh, then a place
        # that reads session.current_balance in-method.
        with coord.active("SOLUSDT"):
            session.refresh_balances(Decimal("5"))
            fake_execute_place()

        # Σ = 100 + 5 + (-2) = 103, NOT the own-symbol 100 + 5 = 105.
        assert captured["wallet_balance"] == Decimal("103")
        assert captured["wallet_balance"] != Decimal("105")

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

    def test_total_position_value_uses_gross_own_symbol_marks(self):
        """Marked combined PV uses gross legs and skips symbols without a mark."""
        session = BacktestSession(initial_balance=Decimal("100"))
        runners = {
            "SOLUSDT": _Runner(
                "0", "0", long_size="2", short_size="3"
            ),
            "LTCUSDT": _Runner(
                "0", "0", long_size="7", short_size="11"
            ),
        }
        coord = _SharedSessionCoordinator(
            session,
            runners,
            {"SOLUSDT": Decimal("10"), "LTCUSDT": Decimal("0")},
        )

        assert coord.total_position_value() == Decimal("50")

    def test_observe_account_risk_latches_once_and_tracks_active_minimum(self):
        """Exact Decimal pool samples latch once and retain later troughs."""
        session = BacktestSession(initial_balance=Decimal("100"))
        runner = _Runner("0", "0")
        idle_runner = _Runner("0", "0")
        coord = _SharedSessionCoordinator(
            session,
            {"SOLUSDT": runner, "LTCUSDT": idle_runner},
            {"SOLUSDT": Decimal("10"), "LTCUSDT": Decimal("10")},
        )
        first_breach = TS + timedelta(milliseconds=1)
        later_breach = TS + timedelta(milliseconds=2)

        with patch("replay.multi_engine.logger.warning") as warning:
            coord.observe_account_risk(TS, Decimal("100"), Decimal("30"), Decimal("10"))
            coord.observe_account_risk(
                first_breach, Decimal("25"), Decimal("30"), Decimal("10")
            )
            coord.observe_account_risk(
                later_breach, Decimal("5"), Decimal("30"), Decimal("10")
            )

        assert coord.min_account_pool == Decimal("-25")
        assert coord.account_halted is True
        assert coord.liq_ts == first_breach
        runner.engine.halt_new_opens.assert_called_once_with()
        idle_runner.engine.halt_new_opens.assert_called_once_with()
        warning.assert_called_once()

    def test_observe_account_risk_ignores_flat_samples_and_flat_breaches(self):
        """Flat accounts retain the Infinity sentinel and never false-latch."""
        session = BacktestSession(initial_balance=Decimal("100"))
        runner = _Runner("0", "0")
        coord = _SharedSessionCoordinator(
            session, {"SOLUSDT": runner}, {"SOLUSDT": Decimal("10")}
        )

        coord.observe_account_risk(TS, Decimal("-1"), Decimal("5"), Decimal("0"))
        coord.observe_account_risk(
            TS + timedelta(milliseconds=1), Decimal("9"), Decimal("2"), Decimal("0")
        )

        assert coord.min_account_pool == Decimal("Infinity")
        assert coord.account_halted is False
        assert coord.liq_ts is None
        runner.engine.halt_new_opens.assert_not_called()


class TestSharedWalletCoupling:
    def test_build_shared_session_seeds_equity_from_coin_balance(self):
        """0095: shared replay initial_equity uses futures cash."""
        config = MultiReplayConfig(
            initial_balance=Decimal("1000"),
            strategies=[
                {"symbol": "SOLUSDT", "strat_id": "sol", "tick_size": "0.01"},
            ],
        )
        wallet_seed = WalletSeed(
            coin_balance=Decimal("314.02"),
            total_available_balance=Decimal("280.00"),
            total_equity=Decimal("324.70"),
            total_margin_balance=Decimal("324.70"),
            account_im_rate=Decimal("0.02"),
            account_mm_rate=Decimal("0.01"),
        )
        session = MultiReplayEngine._build_shared_session(config, wallet_seed)
        assert session.initial_equity == Decimal("314.02")
        assert session.initial_balance == Decimal("280.00")

    def test_cash_baseline_contract_applies_unrealized_once(self):
        """0095: total_equity after update is futures cash plus one U."""
        config = MultiReplayConfig(
            initial_balance=Decimal("1000"),
            strategies=[
                {"symbol": "SOLUSDT", "strat_id": "sol", "tick_size": "0.01"},
            ],
        )
        wallet_seed = WalletSeed(
            coin_balance=Decimal("314.02"),
            total_available_balance=Decimal("280.00"),
            total_equity=Decimal("324.70"),
            total_margin_balance=Decimal("324.70"),
            account_im_rate=Decimal("0.02"),
            account_mm_rate=Decimal("0.01"),
        )
        session = MultiReplayEngine._build_shared_session(config, wallet_seed)
        session.update_equity(TS, Decimal("-8.51"))
        assert session.total_equity == Decimal("305.51")

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


class TestAccountHaltMergedLoop:
    def test_no_breach_retains_active_pool_minimum_and_executes_tick(self):
        """An active but solvent account neither latches nor skips execution."""
        calls: list[str] = []
        runner = _LoopRunner(calls)
        session = BacktestSession(initial_balance=Decimal("100"))
        engine = _loop_engine(session, runner)

        result = engine.run(
            data_providers={"SOLUSDT": InMemoryDataProvider([_tick("SOLUSDT", "100", 0)])}
        )

        assert calls == ["process_fills", "execute_tick"]
        assert result.liquidated is False
        assert result.liq_ts is None
        assert result.min_account_pool == Decimal("50")

    def test_mtm_only_breach_halts_before_process_fills(self):
        """A fresh-price loss breaches at the pre-fill observer sample."""
        calls: list[str] = []
        runner = _LoopRunner(calls)
        session = BacktestSession(initial_balance=Decimal("100"))
        engine = _loop_engine(session, runner)

        result = engine.run(
            data_providers={
                "SOLUSDT": InMemoryDataProvider(
                    [_tick("SOLUSDT", "100", 0), _tick("SOLUSDT", "20", 1)]
                )
            }
        )

        assert calls == [
            "process_fills",
            "execute_tick",
            "halt",
            "process_fills",
            "execute_tick",
        ]
        assert result.liquidated is True
        assert result.liq_ts == TS + timedelta(milliseconds=1)
        assert result.min_account_pool == Decimal("-30")

    def test_fill_driven_breach_halts_at_post_fill_sample(self):
        """A fill that raises MM breaches and halts before execute_tick."""
        calls: list[str] = []
        runner = _LoopRunner(calls, fill_size=Decimal("3"))
        session = BacktestSession(initial_balance=Decimal("100"))
        engine = _loop_engine(session, runner)

        result = engine.run(
            data_providers={"SOLUSDT": InMemoryDataProvider([_tick("SOLUSDT", "100", 0)])}
        )

        assert calls == ["process_fills", "halt", "execute_tick"]
        assert result.liquidated is True
        assert result.liq_ts == TS
        assert result.min_account_pool == Decimal("-50")
        assert result.min_account_pool <= 0


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


class TestAccountHaltEventFollowerIntegration:
    """0102's real event-follower path, with two real GridEngines."""

    @patch("backtest.instrument_info.InstrumentInfoProvider")
    def test_breach_halts_idle_engine_blocks_reactive_opens_and_keeps_closes(
        self, mock_provider_cls, db, seeded_run_account
    ):
        """A real shared-pool breach freezes both engines before the recorded
        SOL fill's synthetic ticker. The organic 100 -> 1 mark move on a
        seeded long deterministically drives the pool below zero; no risk/MM
        helper is patched.

        The seeded open is deliberately pre-halt. It is filled from a real
        ``PrivateExecution`` row, which makes ``process_fills`` dispatch the
        real GridEngine synthetic-ticker reactive path at ``fill_ts``. The
        seeded long is larger than the fixed-USDT close quantity, so the
        runner's real ``_should_place_close`` gate admits a reduce-only close.
        """
        mock_info = MagicMock()
        mock_info.qty_step = Decimal("0.001")
        mock_info.tick_size = Decimal("0.01")
        mock_info.round_qty = lambda q: max(
            Decimal("0.001"), q.quantize(Decimal("0.001"))
        )
        mock_provider_cls.return_value.get.return_value = mock_info

        breach_tick_ts = TS + timedelta(milliseconds=200)
        fill_ts = TS + timedelta(milliseconds=150)
        with db.get_session() as session:
            session.add(
                PrivateExecution(
                    run_id="test-run-id",
                    account_id=seeded_run_account.account_id,
                    symbol="SOLUSDT",
                    exec_id="sol-prehalt-open-fill",
                    order_id="sol-prehalt-open",
                    order_link_id=None,
                    exchange_ts=fill_ts,
                    side="Buy",
                    exec_price=Decimal("99"),
                    exec_qty=Decimal("1"),
                    exec_fee=Decimal("0"),
                    closed_pnl=Decimal("0"),
                )
            )
            session.commit()

        config = MultiReplayConfig(
            run_id="test-run-id",
            start_ts=TS,
            end_ts=TS + timedelta(seconds=1),
            initial_balance=Decimal("100"),
            enable_funding=False,
            fill_simulator={"mode": "event_follower"},
            strategies=[
                {"symbol": "SOLUSDT", "strat_id": "solusdt_halt",
                 "tick_size": "0.01", "grid_count": 4, "grid_step": 1,
                 "amount": "5", "enable_risk_multipliers": False},
                {"symbol": "LTCUSDT", "strat_id": "ltcusdt_halt",
                 "tick_size": "0.01", "grid_count": 4, "grid_step": 1,
                 "amount": "5", "enable_risk_multipliers": False},
            ],
        )
        zero_short = PositionStateSeed(
            direction="short", size=Decimal("0"),
            entry_price=Decimal("0"), liquidation_price=Decimal("0"),
        )
        sol_long = PositionStateSeed(
            direction="long", size=Decimal("20"),
            entry_price=Decimal("100"), liquidation_price=Decimal("0"),
        )
        restored_grid = GridStateSeed(
            strat_id="solusdt_halt",
            grid=[
                {"side": "Buy", "price": Decimal("98")},
                {"side": "Buy", "price": Decimal("99")},
                {"side": "Wait", "price": Decimal("100")},
                {"side": "Sell", "price": Decimal("101")},
                {"side": "Sell", "price": Decimal("102")},
            ],
            grid_step=Decimal("1"),
            grid_count=4,
        )
        seed_data = {
            "SOLUSDT": (
                sol_long,
                zero_short,
                restored_grid,
                [
                    ActiveOrderSeed(
                        client_id="sol-prehalt-open",
                        exchange_order_id="sol-prehalt-open",
                        symbol="SOLUSDT",
                        side="Buy",
                        direction="long",
                        price=Decimal("99"),
                        remaining_qty=Decimal("1"),
                        reduce_only=False,
                        exchange_ts=TS,
                    )
                ],
            ),
            "LTCUSDT": (None, zero_short, None, []),
        }
        providers = {
            "SOLUSDT": InMemoryDataProvider([
                _tick("SOLUSDT", "100", 0),
                _tick("SOLUSDT", "1", 200),
            ]),
            # LTC has an initial mark/build tick but is idle on the SOL breach.
            "LTCUSDT": InMemoryDataProvider([_tick("LTCUSDT", "80", 0)]),
        }
        call_order: list[tuple] = []
        original_halt = GridEngine.halt_new_opens
        original_execute_place = BacktestExecutor.execute_place

        def halt_spy(grid_engine):
            call_order.append(("halt", grid_engine.symbol))
            return original_halt(grid_engine)

        def execute_place_spy(executor, intent, timestamp, wallet_balance):
            result = original_execute_place(
                executor, intent, timestamp, wallet_balance
            )
            call_order.append(
                ("place", intent.symbol, intent.reduce_only, timestamp, result.success)
            )
            return result

        engine = MultiReplayEngine(config=config, db=db)
        with (
            patch.object(MultiReplayEngine, "_load_multi_seed",
                         return_value=(None, seed_data)),
            patch.object(GridEngine, "halt_new_opens", autospec=True,
                         side_effect=halt_spy),
            patch.object(BacktestExecutor, "execute_place", autospec=True,
                         side_effect=execute_place_spy),
        ):
            result = engine.run(data_providers=providers)

        runners = {
            symbol: strategy_result.runner
            for symbol, strategy_result in result.strategies.items()
        }
        assert result.fill_mode == FillMode.EVENT_FOLLOWER
        assert result.liquidated is True
        assert result.liq_ts == breach_tick_ts
        assert runners["SOLUSDT"].engine._new_opens_halted is True
        assert runners["LTCUSDT"].engine._new_opens_halted is True
        assert [entry for entry in call_order if entry[0] == "halt"] == [
            ("halt", "SOLUSDT"),
            ("halt", "LTCUSDT"),
        ]

        first_halt = next(
            index for index, entry in enumerate(call_order) if entry[0] == "halt"
        )
        pre_halt_places = [
            entry for entry in call_order[:first_halt] if entry[0] == "place"
        ]
        post_halt_places = [
            entry for entry in call_order[first_halt:] if entry[0] == "place"
        ]
        # Pre-halt grid opens are a baseline, not post-halt opens.
        assert any(not entry[2] and entry[4] for entry in pre_halt_places)
        assert all(entry[2] for entry in post_halt_places)
        # The timestamp is the recorded fill's exchange time, proving this
        # success came from process_fills' synthetic reactive ticker, not the
        # outer breach tick's later execute_tick call.
        assert any(
            entry[1] == "SOLUSDT"
            and entry[2]
            and entry[3] == fill_ts.replace(tzinfo=None)
            and entry[4]
            for entry in post_halt_places
        )


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
        executed_places = []
        original_execute_place = BacktestExecutor.execute_place

        def execute_place_spy(executor, intent, timestamp, wallet_balance):
            result = original_execute_place(
                executor, intent, timestamp, wallet_balance
            )
            executed_places.append((intent, result))
            return result

        engine = MultiReplayEngine(config=config, db=db)
        with patch.object(
            BacktestExecutor,
            "execute_place",
            autospec=True,
            side_effect=execute_place_spy,
        ):
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
        # 0102: result exposes direct non-breach account-halt fields.
        assert result.liquidated is False
        assert result.liq_ts is None
        assert any(
            not intent.reduce_only and place_result.success
            for intent, place_result in executed_places
        )
        # The run opens a real position, so this must be an observed pool
        # sample rather than the no-position Infinity sentinel.
        assert result.min_account_pool != Decimal("Infinity")
        assert result.min_account_pool > 0
        assert isinstance(result.min_account_pool, Decimal)

    @patch("backtest.instrument_info.InstrumentInfoProvider")
    def test_run_subtracts_summed_u0_from_balance_only(
        self, mock_provider_cls, db, seeded_run_account
    ):
        """0101 multi-engine asymmetry: seeded shared session subtracts the
        SUMMED per-strategy U0 from ``initial_balance`` (TAB) ONLY;
        ``initial_equity`` (``coin_balance`` — cash, no embedded UPL) is
        UNCHANGED. Subtracting there would invert the bug on the equity axis.

        Driven through production ``run()`` so the flatten→compute→thread
        wiring (multi_engine.py run() → _build_shared_session) is exercised;
        a hand-built _build_shared_session(config, wallet_seed, u0) call would
        pass even if run() never flattened the legs.

        SOL long U0=10, LTC long U0=25 → summed U0 = 35.
          initial_balance = 280.00 - 35 = 245.00
          initial_equity  = 314.02          (coin_balance, UNCHANGED)
        """
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
                 "tick_size": "0.01", "grid_count": 4, "grid_step": 0.5},
                {"symbol": "LTCUSDT", "strat_id": "ltcusdt_test",
                 "tick_size": "0.01", "grid_count": 4, "grid_step": 0.5},
            ],
        )
        wallet_seed = WalletSeed(
            coin_balance=Decimal("314.02"),
            total_available_balance=Decimal("280.00"),
            total_equity=Decimal("324.70"),
            total_margin_balance=Decimal("324.70"),
            account_im_rate=Decimal("0.02"),
            account_mm_rate=Decimal("0.01"),
        )
        zero_short = PositionStateSeed(
            direction="short", size=Decimal("0"),
            entry_price=Decimal("0"), liquidation_price=Decimal("0"),
        )
        seed_data = {
            "SOLUSDT": (
                PositionStateSeed(
                    direction="long", size=Decimal("2"),
                    entry_price=Decimal("95"), liquidation_price=Decimal("0"),
                    unrealised_pnl=Decimal("10"),
                ),
                zero_short,
                GridStateSeed(
                    strat_id="solusdt_test",
                    grid=[
                        {"side": "Buy", "price": 99.0},
                        {"side": "Buy", "price": 99.5},
                        {"side": "Sell", "price": 100.5},
                        {"side": "Sell", "price": 101.0},
                    ],
                    grid_step=0.5, grid_count=4,
                ),
                [],
            ),
            "LTCUSDT": (
                PositionStateSeed(
                    direction="long", size=Decimal("3"),
                    entry_price=Decimal("75"), liquidation_price=Decimal("0"),
                    unrealised_pnl=Decimal("25"),
                ),
                zero_short,
                GridStateSeed(
                    strat_id="ltcusdt_test",
                    grid=[
                        {"side": "Buy", "price": 79.2},
                        {"side": "Buy", "price": 79.6},
                        {"side": "Sell", "price": 80.4},
                        {"side": "Sell", "price": 80.8},
                    ],
                    grid_step=0.5, grid_count=4,
                ),
                [],
            ),
        }
        providers = {
            "SOLUSDT": InMemoryDataProvider([_tick("SOLUSDT", "100", 0)]),
            "LTCUSDT": InMemoryDataProvider([_tick("LTCUSDT", "80", 100)]),
        }
        engine = MultiReplayEngine(config=config, db=db)
        with patch.object(
            MultiReplayEngine, "_load_multi_seed",
            return_value=(wallet_seed, seed_data),
        ):
            result = engine.run(data_providers=providers)

        assert result.session.initial_balance == Decimal("245.00")
        assert result.session.initial_equity == Decimal("314.02")
