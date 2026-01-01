"""Tests for BacktestSession and related data structures"""

from datetime import datetime

from src.backtest_session import BacktestPositionSnapshot, BacktestSession, BacktestTrade


def test_backtest_session_creation():
    """Test basic session creation"""
    session = BacktestSession("TEST_001")
    
    assert session.session_id == "TEST_001"
    assert session.initial_balance == 10000.0
    assert session.current_balance == 10000.0
    assert len(session.trades) == 0
    assert len(session.position_snapshots) == 0


def test_record_trade():
    """Test recording trades"""
    session = BacktestSession("TEST_002")
    
    trade = BacktestTrade(
        trade_id="TRADE_001",
        symbol="BTCUSDT",
        side="Buy",
        size=0.001,
        price=50000.0,
        direction="long",
        executed_at=datetime.now(),
        order_id="ORDER_001",
        strategy_id=1,
        bm_name="test_bm",
        realized_pnl=10.0
    )
    
    session.record_trade(trade)
    
    assert len(session.trades) == 1
    assert session.current_balance == 10010.0  # Initial + PnL
    assert session.trades[0].trade_id == "TRADE_001"


def test_record_position_snapshot():
    """Test recording position snapshots"""
    session = BacktestSession("TEST_003")
    
    snapshot = BacktestPositionSnapshot(
        timestamp=datetime.now(),
        symbol="BTCUSDT",
        direction="long",
        size=0.001,
        entry_price=50000.0,
        current_price=50100.0,
        unrealized_pnl=0.1,
        margin=500.0,
        liquidation_price=45000.0
    )
    
    session.record_position_snapshot(snapshot)
    
    assert len(session.position_snapshots) == 1
    assert session.position_snapshots[0].symbol == "BTCUSDT"


def test_metrics_calculation():
    """Test metrics calculation"""
    session = BacktestSession("TEST_004")
    
    # Add some trades
    trades = [
        BacktestTrade("T1", "BTCUSDT", "Buy", 0.001, 50000, "long", datetime.now(), "O1", 1, "bm1", 10.0),
        BacktestTrade("T2", "BTCUSDT", "Sell", 0.001, 50500, "long", datetime.now(), "O2", 1, "bm1", -5.0),
        BacktestTrade("T3", "BTCUSDT", "Buy", 0.001, 51000, "long", datetime.now(), "O3", 1, "bm1", 15.0),
    ]
    
    for trade in trades:
        session.record_trade(trade)
    
    metrics = session.get_final_metrics()
    
    assert "BTCUSDT" in metrics
    btc_metrics = metrics["BTCUSDT"]
    
    assert btc_metrics.total_trades == 3
    assert btc_metrics.winning_trades == 2
    assert btc_metrics.total_pnl == 20.0
    assert abs(btc_metrics.win_rate - 66.67) < 0.1


def test_session_summary():
    """Test session summary"""
    session = BacktestSession("TEST_005")
    
    # Add a trade
    trade = BacktestTrade("T1", "ETHUSDT", "Buy", 0.1, 3000, "long", datetime.now(), "O1", 1, "bm1", 50.0)
    session.record_trade(trade)
    
    summary = session.get_summary()
    
    assert summary['session_id'] == "TEST_005"
    assert summary['total_trades'] == 1
    assert summary['winning_trades'] == 1
    assert summary['win_rate'] == 100.0
    assert summary['total_pnl'] == 50.0
    assert summary['current_balance'] == 10050.0
    assert summary['return_pct'] == 0.5  # 50/10000 * 100
    assert "ETHUSDT" in summary['symbols']


if __name__ == "__main__":
    # Run a simple test
    session = BacktestSession("MANUAL_TEST")
    
    print("Testing BacktestSession...")
    
    # Add some test data
    trade1 = BacktestTrade("T1", "BTCUSDT", "Buy", 0.001, 50000, "long", datetime.now(), "O1", 1, "bm1", 25.0)
    trade2 = BacktestTrade("T2", "BTCUSDT", "Sell", 0.001, 50500, "long", datetime.now(), "O2", 1, "bm1", -10.0)
    
    session.record_trade(trade1)
    session.record_trade(trade2)
    
    # Print summary
    session.print_summary()
    
    # Get metrics
    metrics = session.get_final_metrics()
    print(f"Calculated metrics for {len(metrics)} symbols")
    
    print("✅ BacktestSession tests passed!")
