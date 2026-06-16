"""End-to-end integration test for 0065 collateral re-marking.

Synthetic in-memory window: a traded symbol (BTCUSDT) that does not move
(no fills → futures pnl_delta == 0) plus a SOL collateral coin whose mark
floats 80 → 90 over the window. Verifies acceptance #3a (totalEquity parity
within $1) and #3b (drift attribution) through the full ``ReplayEngine.run``
loop — feed → ``update_collateral_mark`` → session ``total_equity`` →
``ValidationMetrics``.

Acceptance #1 (liq_price delta shrink vs un-fixed baseline) is exercised by
the real-data ``scripts/verify_0065_collateral.py`` against
``recorder_ltcusdt_phase4.db`` — it needs paired live/backtest position
snapshots from a real fill stream, which a synthetic no-fill window cannot
produce. The mechanism (total_equity feeds the pair-liq pool) is covered by
the session unit tests and the runner anchors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from grid_db import (
    BybitAccount,
    PositionSnapshot,
    PositionSnapshotRepository,
    Run,
    Strategy,
    TickerSnapshot,
    TickerSnapshotRepository,
    User,
    WalletSnapshot,
    WalletSnapshotRepository,
)

from gridcore.persistence import GridStateStore

from replay.config import ReplayConfig, ReplayStrategyConfig, SeedConfig
from replay.engine import ReplayEngine


SYMBOL = "BTCUSDT"
STRAT_ID = "ltcusdt_test"
AT_TS = datetime(2026, 6, 1, 17, 42, 0, tzinfo=timezone.utc)
END_TS = AT_TS + timedelta(minutes=10)
SNAP_TS = AT_TS - timedelta(seconds=60)

# Collateral scenario: 0.25 SOL re-marked 80 -> 90 => +2.5 USD on total_equity.
SOL_BALANCE = Decimal("0.25")
SOL_SEED_MARK = Decimal("80")
SOL_END_MARK = Decimal("90")
EXPECTED_DRIFT = SOL_BALANCE * (SOL_END_MARK - SOL_SEED_MARK)  # 2.5
SEED_TOTAL_EQUITY = Decimal("15000.50")
LIVE_END_TOTAL_EQUITY = SEED_TOTAL_EQUITY + EXPECTED_DRIFT  # 15003.00


def _ticker(symbol, ts, price):
    return TickerSnapshot(
        symbol=symbol, exchange_ts=ts, local_ts=ts,
        last_price=price, mark_price=price,
        bid1_price=price, ask1_price=price, funding_rate=Decimal("0.0001"),
    )


@pytest.fixture
def grid_state_path(tmp_path):
    path = tmp_path / "grid.json"
    store = GridStateStore(file_path=str(path))
    store.save(
        STRAT_ID,
        [
            {"side": "Buy", "price": 99600.0},
            {"side": "Buy", "price": 99800.0},
            {"side": "Sell", "price": 100200.0},
            {"side": "Sell", "price": 100400.0},
        ],
        grid_step=0.2, grid_count=4,
    )
    store.flush()
    return str(path)


@pytest.fixture
def collateral_db(db):
    """Seed run + USDT/SOL wallet + flat positions + BTC & SOL ticker rows."""
    with db.get_session() as session:
        session.add_all([
            User(user_id="user-1", username="collat"),
            BybitAccount(
                account_id="acc-1", user_id="user-1",
                account_name="seed", environment="testnet",
            ),
            Strategy(
                strategy_id="strat-1", account_id="acc-1",
                strategy_type="recorder", symbol=SYMBOL, config_json={},
            ),
            Run(
                run_id="seed-run", user_id="user-1", account_id="acc-1",
                strategy_id="strat-1", run_type="recording", status="running",
                start_ts=SNAP_TS - timedelta(minutes=5),
                end_ts=END_TS + timedelta(hours=1),
            ),
        ])
        session.commit()

        # Flat positions (both sides zero → no open exposure, pnl stays 0).
        PositionSnapshotRepository(session).bulk_insert([
            PositionSnapshot(
                run_id="seed-run", account_id="acc-1", symbol=SYMBOL,
                exchange_ts=SNAP_TS, local_ts=SNAP_TS,
                side="Buy", size=Decimal("0"), entry_price=Decimal("0"),
                liq_price=None,
            ),
            PositionSnapshot(
                run_id="seed-run", account_id="acc-1", symbol=SYMBOL,
                exchange_ts=SNAP_TS, local_ts=SNAP_TS,
                side="Sell", size=Decimal("0"), entry_price=Decimal("0"),
                liq_price=None,
            ),
        ])

        WalletSnapshotRepository(session).bulk_insert([
            # Seed USDT row (at_ts anchor).
            WalletSnapshot(
                run_id="seed-run", account_id="acc-1",
                exchange_ts=SNAP_TS, local_ts=SNAP_TS, coin="USDT",
                wallet_balance=Decimal("12345.67"),
                available_balance=Decimal("12000.00"),
                total_equity=SEED_TOTAL_EQUITY,
                total_available_balance=Decimal("14000.25"),
                total_margin_balance=Decimal("14900.75"),
                account_im_rate=Decimal("0.01"), account_mm_rate=Decimal("0.005"),
            ),
            # Live end-of-window USDT row (floated with SOL) for #3a.
            WalletSnapshot(
                run_id="seed-run", account_id="acc-1",
                exchange_ts=END_TS, local_ts=END_TS, coin="USDT",
                wallet_balance=Decimal("12345.67"),
                available_balance=Decimal("12000.00"),
                total_equity=LIVE_END_TOTAL_EQUITY,
                total_available_balance=Decimal("14000.25"),
                total_margin_balance=Decimal("14900.75"),
                account_im_rate=Decimal("0.01"), account_mm_rate=Decimal("0.005"),
            ),
            # SOL collateral row at seed (fresh usdValue → seed mark 80).
            WalletSnapshot(
                run_id="seed-run", account_id="acc-1",
                exchange_ts=SNAP_TS, local_ts=SNAP_TS, coin="SOL",
                wallet_balance=SOL_BALANCE, available_balance=SOL_BALANCE,
                raw_json={
                    "coin": "SOL", "walletBalance": str(SOL_BALANCE),
                    "usdValue": str(SOL_BALANCE * SOL_SEED_MARK),  # 20.00
                    "collateralSwitch": True, "marginCollateral": True,
                },
            ),
        ])

        # Traded ticks: BTC flat at 100000 (no grid cross → no fills).
        TickerSnapshotRepository(session).bulk_insert([
            _ticker(SYMBOL, AT_TS, Decimal("100000")),
            _ticker(SYMBOL, AT_TS + timedelta(minutes=5), Decimal("100000")),
            _ticker(SYMBOL, AT_TS + timedelta(minutes=9), Decimal("100000")),
            # Collateral ticks: SOL 80 -> 90 (90 lands before the +5min tick).
            _ticker("SOLUSDT", AT_TS, SOL_SEED_MARK),
            _ticker("SOLUSDT", AT_TS + timedelta(minutes=4), SOL_END_MARK),
        ])
        session.commit()
    return db


@pytest.fixture
def mock_instrument():
    with patch("replay.engine.InstrumentInfoProvider") as mock_cls:
        info = MagicMock()
        info.qty_step = Decimal("0.001")
        info.tick_size = Decimal("0.1")
        info.round_qty = lambda q: max(Decimal("0.001"), q.quantize(Decimal("0.001")))
        mock_cls.return_value.get.return_value = info
        yield info


def _config(grid_state_path):
    return ReplayConfig(
        database_url="sqlite:///:memory:",
        run_id="seed-run", symbol=SYMBOL,
        start_ts=AT_TS, end_ts=END_TS,
        strategy=ReplayStrategyConfig(
            tick_size=Decimal("0.1"), grid_count=4, grid_step=0.2,
            enable_risk_multipliers=True,
        ),
        initial_balance=Decimal("10000"), enable_funding=False,
        seed=SeedConfig(
            enabled=True, at_ts=AT_TS, account_id="acc-1", strat_id=STRAT_ID,
            grid_state_path=grid_state_path, wallet_coin="USDT",
            collateral_coins=["SOL"], collateral_symbol_map={"SOL": "SOLUSDT"},
        ),
    )


class TestCollateralRemarkIntegration:
    def test_3b_drift_attribution(self, collateral_db, grid_state_path, mock_instrument):
        """#3b: metric == session drift == balance * (end_mark - seed_mark)."""
        result = ReplayEngine(config=_config(grid_state_path), db=collateral_db).run()
        assert result.session.collateral_drift_total == EXPECTED_DRIFT
        assert result.metrics.non_usdt_collateral_drift_total == EXPECTED_DRIFT
        assert result.metrics.collateral_drift_by_coin == {"SOL": EXPECTED_DRIFT}
        assert result.metrics.collateral_excluded_coins == []
        assert result.metrics.collateral_missing_mark_coins == []

    def test_3a_total_equity_parity(self, collateral_db, grid_state_path, mock_instrument):
        """#3a: |live_total_equity(end) - backtest total_equity| < $1 (leave_open)."""
        result = ReplayEngine(config=_config(grid_state_path), db=collateral_db).run()
        backtest_total_equity = result.session.total_equity

        # Live end-of-window totalEquity via the plan's procedure.
        with collateral_db.get_session() as s:
            row = WalletSnapshotRepository(s).get_latest_before(
                "seed-run", "acc-1", "USDT", END_TS,
            )
            live_total_equity = row.total_equity

        assert live_total_equity == LIVE_END_TOTAL_EQUITY
        assert abs(live_total_equity - backtest_total_equity) < Decimal("1")
        # Mechanism: backtest moved by exactly the collateral re-mark.
        assert backtest_total_equity == SEED_TOTAL_EQUITY + EXPECTED_DRIFT

    def test_usdt_only_run_is_noop(self, collateral_db, grid_state_path, mock_instrument):
        """#4: empty collateral_coins → no drift, total_equity == seed equity."""
        cfg = _config(grid_state_path)
        cfg = cfg.model_copy(
            update={"seed": cfg.seed.model_copy(update={"collateral_coins": []})}
        )
        result = ReplayEngine(config=cfg, db=collateral_db).run()
        assert result.session.collateral_drift_total == Decimal("0")
        assert result.metrics.non_usdt_collateral_drift_total == Decimal("0")
        assert result.session.total_equity == SEED_TOTAL_EQUITY

    def test_multi_coin_drift_and_excluded(
        self, collateral_db, grid_state_path, mock_instrument
    ):
        """#3b end-to-end with TWO modelled coins (SOL +2.5, ETH +100) and one
        EXCLUDED coin (DOGE, no wallet row) — drift_by_coin + excluded_coins
        plumb correctly through engine.run()."""
        # Add an ETH collateral row (fresh usdValue → seed mark 2000) + ETHUSDT
        # ticker floating 2000 -> 2100 (+100 on 1.0 ETH). DOGE has no row.
        with collateral_db.get_session() as session:
            WalletSnapshotRepository(session).bulk_insert([
                WalletSnapshot(
                    run_id="seed-run", account_id="acc-1",
                    exchange_ts=SNAP_TS, local_ts=SNAP_TS, coin="ETH",
                    wallet_balance=Decimal("1.0"), available_balance=Decimal("1.0"),
                    raw_json={
                        "coin": "ETH", "walletBalance": "1.0",
                        "usdValue": "2000", "collateralSwitch": True,
                        "marginCollateral": True,
                    },
                ),
            ])
            TickerSnapshotRepository(session).bulk_insert([
                _ticker("ETHUSDT", AT_TS, Decimal("2000")),
                _ticker("ETHUSDT", AT_TS + timedelta(minutes=4), Decimal("2100")),
            ])
            session.commit()

        cfg = _config(grid_state_path)
        cfg = cfg.model_copy(update={"seed": cfg.seed.model_copy(update={
            "collateral_coins": ["SOL", "ETH", "DOGE"],
            "collateral_symbol_map": {"SOL": "SOLUSDT", "ETH": "ETHUSDT"},
        })})
        result = ReplayEngine(config=cfg, db=collateral_db).run()

        assert result.metrics.collateral_drift_by_coin == {
            "SOL": EXPECTED_DRIFT, "ETH": Decimal("100"),
        }
        assert result.metrics.non_usdt_collateral_drift_total == EXPECTED_DRIFT + Decimal("100")
        assert result.session.collateral_drift_total == EXPECTED_DRIFT + Decimal("100")
        assert result.metrics.collateral_excluded_coins == ["DOGE"]
        assert result.metrics.collateral_missing_mark_coins == []
