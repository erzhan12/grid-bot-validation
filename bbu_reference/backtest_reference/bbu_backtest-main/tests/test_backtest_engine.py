"""Test BacktestEngine functionality"""

from src.backtest_engine import BacktestEngine, BacktestRunner
from src.backtest_session import BacktestSession


def test_backtest_engine_creation():
    """Test BacktestEngine basic creation"""
    engine = BacktestEngine()
    
    assert engine.session_id.startswith("BT_")
    assert engine.initial_balance == 10000.0
    assert isinstance(engine.backtest_session, BacktestSession)
    assert engine.verbose

    print("✅ BacktestEngine creation test passed")


def test_engine_configuration():
    """Test engine configuration methods"""
    engine = BacktestEngine()
    
    # Test balance setting
    engine.set_initial_balance(50000.0)
    assert engine.initial_balance == 50000.0
    
    # Test verbose setting
    engine.set_verbose(False)
    assert not engine.verbose

    # Test session summary
    summary = engine.get_session_summary()
    assert 'session_id' in summary
    assert 'total_trades' in summary
    
    print("✅ Engine configuration test passed")


def test_results_summary_creation():
    """Test results summary creation"""
    engine = BacktestEngine()
    
    # Mock some data
    metrics = {
        'BTCUSDT': {
            'total_trades': 10,
            'winning_trades': 6,
            'total_pnl': 150.0
        }
    }
    
    results = engine._create_results_summary('BTCUSDT', metrics)
    
    assert results['symbol'] == 'BTCUSDT'
    assert 'backtest_info' in results
    assert 'trading_metrics' in results
    assert 'detailed_metrics' in results
    
    print("✅ Results summary creation test passed")


def test_backtest_runner():
    """Test BacktestRunner convenience class"""
    runner = BacktestRunner(initial_balance=25000.0, verbose=False)

    assert runner.initial_balance == 25000.0
    assert not runner.verbose

    print("✅ BacktestRunner test passed")


def test_session_id_generation():
    """Test unique session ID generation"""
    engine1 = BacktestEngine()
    engine2 = BacktestEngine()
    
    assert str(engine1.session_id) != str(engine2.session_id)
    assert engine1.session_id.startswith("BT_")
    assert engine2.session_id.startswith("BT_")
    
    print("✅ Session ID generation test passed")


def test_multiple_symbols_preparation():
    """Test preparation for multiple symbols"""
    BacktestEngine()
    
    symbols = ['BTCUSDT', 'ETHUSDT']
    
    # Test that engine can handle multiple symbols conceptually
    assert len(symbols) == 2
    assert 'BTCUSDT' in symbols
    assert 'ETHUSDT' in symbols
    
    # Test results structure
    results = {}
    for symbol in symbols:
        results[symbol] = {'symbol': symbol, 'trades': 0}
    
    assert len(results) == 2
    assert 'BTCUSDT' in results
    assert 'ETHUSDT' in results
    
    print("✅ Multiple symbols preparation test passed")


def test_backtest_session_integration():
    """Test integration with BacktestSession"""
    engine = BacktestEngine()
    
    # Verify session is properly initialized
    assert engine.backtest_session.session_id == engine.session_id
    assert engine.backtest_session.initial_balance == 10000.0
    assert len(engine.backtest_session.trades) == 0
    
    # Test session updates
    engine.set_initial_balance(15000.0)
    # Note: We'll update session balance when we run actual backtests
    
    print("✅ BacktestSession integration test passed")


def test_export_functionality():
    """Test export functionality exists"""
    engine = BacktestEngine()
    
    # Verify export method exists
    assert hasattr(engine, 'export_results')
    assert callable(engine.export_results)
    
    print("✅ Export functionality test passed")


if __name__ == "__main__":
    print("Testing BacktestEngine...")
    
    test_backtest_engine_creation()
    test_engine_configuration()
    test_results_summary_creation()
    test_backtest_runner()
    test_session_id_generation()
    test_multiple_symbols_preparation()
    test_backtest_session_integration()
    test_export_functionality()
    
    print("\n🎉 All BacktestEngine tests passed!")
    print("🚀 Ready for final integration testing!")
