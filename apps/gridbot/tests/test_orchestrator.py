"""Tests for gridbot orchestrator module."""

import threading
import time
from decimal import Decimal
from unittest.mock import Mock, MagicMock, patch

import pytest

from gridbot.config import GridbotConfig, AccountConfig, StrategyConfig
from gridbot.notifier import Notifier
from gridbot.orchestrator import Orchestrator
from gridbot.reconciler import ReconciliationResult


@pytest.fixture
def account_config():
    """Sample account configuration."""
    return AccountConfig(
        name="test_account",
        api_key="test_key",
        api_secret="test_secret",
        testnet=True,
    )


@pytest.fixture
def strategy_config():
    """Sample strategy configuration."""
    return StrategyConfig(
        strat_id="btcusdt_test",
        account="test_account",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=20,
        grid_step=0.2,
        shadow_mode=False,
    )


@pytest.fixture
def gridbot_config(account_config, strategy_config):
    """Sample gridbot configuration."""
    return GridbotConfig(
        accounts=[account_config],
        strategies=[strategy_config],
        database_url="sqlite:///:memory:",
        position_check_interval=60.0,
    )


class TestOrchestratorBasic:
    """Basic tests for Orchestrator."""

    def test_create_orchestrator(self, gridbot_config):
        """Test creating orchestrator."""
        orchestrator = Orchestrator(gridbot_config)

        assert orchestrator.running is False
        assert len(orchestrator._runners) == 0


class TestOrchestratorInit:
    """Tests for orchestrator initialization."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_init_account(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
        account_config,
    ):
        """Test account initialization."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)

        assert "test_account" in orchestrator._rest_clients
        assert "test_account" in orchestrator._executors
        assert "test_account" in orchestrator._reconcilers
        assert "test_account" in orchestrator._public_ws
        assert "test_account" in orchestrator._private_ws

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_init_strategy(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
        account_config,
        strategy_config,
    ):
        """Test strategy initialization."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)

        assert "btcusdt_test" in orchestrator._runners
        assert "btcusdt_test" in orchestrator._retry_queues


class TestOrchestratorRouting:
    """Tests for event routing."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_build_routing_maps(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
        account_config,
        strategy_config,
    ):
        """Test routing map construction."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        # Check symbol routing
        assert "BTCUSDT" in orchestrator._symbol_to_runners
        assert len(orchestrator._symbol_to_runners["BTCUSDT"]) == 1

        # Check account routing
        assert "test_account" in orchestrator._account_to_runners
        assert len(orchestrator._account_to_runners["test_account"]) == 1

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_get_account_for_strategy(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
    ):
        """Test getting account for strategy."""
        orchestrator = Orchestrator(gridbot_config)

        account = orchestrator._get_account_for_strategy("btcusdt_test")
        assert account == "test_account"

        account = orchestrator._get_account_for_strategy("nonexistent")
        assert account is None


class TestOrchestratorMultipleStrategies:
    """Tests with multiple strategies."""

    @pytest.fixture
    def multi_config(self, account_config):
        """Config with multiple strategies."""
        strategies = [
            StrategyConfig(
                strat_id="btcusdt_test",
                account="test_account",
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
            ),
            StrategyConfig(
                strat_id="ethusdt_test",
                account="test_account",
                symbol="ETHUSDT",
                tick_size=Decimal("0.01"),
            ),
        ]
        return GridbotConfig(
            accounts=[account_config],
            strategies=strategies,
        )

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_multiple_strategies_same_account(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        multi_config,
        account_config,
    ):
        """Test multiple strategies on same account."""
        orchestrator = Orchestrator(multi_config)
        orchestrator._init_account(account_config)

        for strategy in multi_config.strategies:
            orchestrator._init_strategy(strategy)

        orchestrator._build_routing_maps()

        # Should have 2 runners
        assert len(orchestrator._runners) == 2

        # Account should have 2 runners
        assert len(orchestrator._account_to_runners["test_account"]) == 2

        # Each symbol should have 1 runner
        assert len(orchestrator._symbol_to_runners["BTCUSDT"]) == 1
        assert len(orchestrator._symbol_to_runners["ETHUSDT"]) == 1


class TestOrchestratorLifecycle:
    """Tests for orchestrator start()/stop() lifecycle."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_start_sets_running(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
    ):
        """start() sets running flag; stop() clears it."""
        mock_public_ws.return_value.connect = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)
        orchestrator.start()

        assert orchestrator.running is True

        orchestrator.stop()
        assert orchestrator.running is False

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_stop_disconnects_websockets(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
    ):
        """stop() disconnects all WebSockets."""
        mock_public_ws.return_value.connect = Mock()
        mock_public_ws.return_value.disconnect = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_private_ws.return_value.disconnect = Mock()
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)
        orchestrator.start()
        orchestrator.stop()

        mock_public_ws.return_value.disconnect.assert_called()
        mock_private_ws.return_value.disconnect.assert_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_start_calls_initial_position_fetch(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
    ):
        """start() fetches positions before entering the main loop."""
        mock_public_ws.return_value.connect = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)

        with patch.object(
            orchestrator, "_fetch_and_update_positions"
        ) as mock_fetch:
            orchestrator.start()
            mock_fetch.assert_called_once_with(startup=True)

        orchestrator.stop()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_start_position_fetch_exception_handling(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
        account_config,
        strategy_config,
    ):
        """start() completes even when wallet balance fetch raises."""
        mock_public_ws.return_value.connect = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        # Mock wallet balance to raise (startup warns and continues)
        orchestrator._get_wallet_balance = Mock(
            side_effect=TimeoutError("REST hung")
        )

        # start() should complete without raising
        orchestrator.start()
        assert orchestrator.running is True

        orchestrator.stop()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_start_rest_get_positions_exception(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
        account_config,
        strategy_config,
    ):
        """start() continues when REST get_positions raises."""
        mock_public_ws.return_value.connect = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        # Wallet balance succeeds but get_positions raises
        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]
        }
        rest_client.get_positions.side_effect = TimeoutError("REST hung")

        orchestrator.start()
        assert orchestrator.running is True

        orchestrator.stop()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_start_position_update_runner_exception(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
        account_config,
        strategy_config,
    ):
        """start() continues when runner.on_position_update raises."""
        mock_public_ws.return_value.connect = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]
        }
        rest_client.get_positions.return_value = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0"},
            {"symbol": "BTCUSDT", "side": "Sell", "size": "0"},
        ]

        runner = orchestrator._runners["btcusdt_test"]
        runner.on_position_update = Mock(
            side_effect=RuntimeError("runner exploded")
        )

        orchestrator.start()
        assert orchestrator.running is True
        runner.on_position_update.assert_called_once()

        orchestrator.stop()


class TestOrchestratorGuardClauses:
    """Tests for guard clauses in start/stop and request_stop."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_start_already_running_returns_early(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config
    ):
        """start() returns immediately when already running."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._running = True
        orchestrator.start()
        assert len(orchestrator._runners) == 0

    def test_stop_not_started_returns_early(self, gridbot_config):
        """stop() is a no-op when start() was never called."""
        orchestrator = Orchestrator(gridbot_config)
        assert not orchestrator._started
        assert not orchestrator._running
        orchestrator.stop()  # must not raise
        assert not orchestrator._started
        assert not orchestrator._running

    def test_request_stop_clears_running_flag(self, gridbot_config):
        """request_stop() clears the running flag so run() exits cleanly."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._running = True
        orchestrator.request_stop()
        assert orchestrator._running is False

    def test_request_stop_is_idempotent(self, gridbot_config):
        """Calling request_stop() twice is safe (signal handler may fire twice)."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._running = True
        orchestrator.request_stop()
        orchestrator.request_stop()  # must not raise
        assert orchestrator._running is False

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_stop_after_request_stop_still_cleans_up(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config
    ):
        """Bug regression: stop() after request_stop() must still disconnect WS.

        Flow: signal handler fires request_stop() (clearing _running),
        run() exits, then the finally block in main.py calls stop().
        stop() must not skip cleanup just because _running is already
        False — gate is on _started, not _running.
        """
        mock_public_ws.return_value.connect = Mock()
        mock_public_ws.return_value.disconnect = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_private_ws.return_value.disconnect = Mock()
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)
        orchestrator.start()
        assert orchestrator._started is True
        assert orchestrator._running is True

        # Simulate signal-handler path: request_stop() runs first.
        orchestrator.request_stop()
        assert orchestrator._running is False
        assert orchestrator._started is True  # still started, loop just exited

        # Now main.py's finally block calls stop(). It MUST still
        # disconnect the WebSockets and clear _started.
        orchestrator.stop()
        mock_public_ws.return_value.disconnect.assert_called()
        mock_private_ws.return_value.disconnect.assert_called()
        assert orchestrator._started is False

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_stop_is_idempotent(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config
    ):
        """Calling stop() twice must not double-disconnect or raise."""
        mock_public_ws.return_value.connect = Mock()
        mock_public_ws.return_value.disconnect = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_private_ws.return_value.disconnect = Mock()
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)
        orchestrator.start()
        orchestrator.stop()
        mock_public_ws.return_value.disconnect.reset_mock()
        mock_private_ws.return_value.disconnect.reset_mock()

        orchestrator.stop()  # second call is a no-op
        mock_public_ws.return_value.disconnect.assert_not_called()
        mock_private_ws.return_value.disconnect.assert_not_called()


class TestOrchestratorPositionWsCache:
    """Tests for WebSocket position data caching."""

    def test_on_position_stores_linear_data(self, gridbot_config):
        """_on_position stores linear position data in cache."""
        orchestrator = Orchestrator(gridbot_config)
        message = {
            "data": [
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "size": "0.1",
                    "avgPrice": "42500.00",
                }
            ]
        }
        orchestrator._on_position("test_account", message)

        cached = orchestrator._position_ws_data["test_account"]["BTCUSDT"]["Buy"]
        assert cached["size"] == "0.1"
        assert cached["avgPrice"] == "42500.00"

    def test_on_position_stores_both_sides(self, gridbot_config):
        """_on_position stores both Buy and Sell positions."""
        orchestrator = Orchestrator(gridbot_config)
        message = {
            "data": [
                {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "size": "0.1"},
                {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "size": "0.05"},
            ]
        }
        orchestrator._on_position("test_account", message)

        assert orchestrator._position_ws_data["test_account"]["BTCUSDT"]["Buy"]["size"] == "0.1"
        assert orchestrator._position_ws_data["test_account"]["BTCUSDT"]["Sell"]["size"] == "0.05"

    def test_on_position_filters_non_linear(self, gridbot_config):
        """_on_position ignores non-linear positions."""
        orchestrator = Orchestrator(gridbot_config)
        message = {
            "data": [
                {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "size": "1.0"},
            ]
        }
        orchestrator._on_position("test_account", message)
        assert len(orchestrator._position_ws_data.get("test_account", {})) == 0

    def test_on_position_skips_empty_symbol_or_side(self, gridbot_config):
        """_on_position skips entries with empty symbol or side."""
        orchestrator = Orchestrator(gridbot_config)
        message = {
            "data": [
                {"category": "linear", "symbol": "", "side": "Buy", "size": "0.1"},
                {"category": "linear", "symbol": "BTCUSDT", "side": "", "size": "0.1"},
            ]
        }
        orchestrator._on_position("test_account", message)
        account_data = orchestrator._position_ws_data.get("test_account", {})
        assert len(account_data.get("BTCUSDT", {})) == 0

    def test_on_position_handles_empty_data(self, gridbot_config):
        """_on_position handles empty or missing data gracefully."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._on_position("test_account", {"data": []})
        orchestrator._on_position("test_account", {})

    def test_get_position_from_ws_returns_cached_data(self, gridbot_config):
        """_get_position_from_ws returns data when available."""
        orchestrator = Orchestrator(gridbot_config)
        pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"}
        orchestrator._position_ws_data = {"acct": {"BTCUSDT": {"Buy": pos}}}

        result = orchestrator._get_position_from_ws("acct", "BTCUSDT", "Buy")
        assert result == pos

    def test_get_position_from_ws_returns_none_when_missing(self, gridbot_config):
        """_get_position_from_ws returns None for missing data."""
        orchestrator = Orchestrator(gridbot_config)
        assert orchestrator._get_position_from_ws("missing", "BTCUSDT", "Buy") is None

        orchestrator._position_ws_data = {"acct": {}}
        assert orchestrator._get_position_from_ws("acct", "BTCUSDT", "Buy") is None

        orchestrator._position_ws_data = {"acct": {"BTCUSDT": {}}}
        assert orchestrator._get_position_from_ws("acct", "BTCUSDT", "Buy") is None


class TestOrchestratorEventHandlers:
    """Tests for WebSocket event handler buffering (WS→main bridge)."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_on_ticker_caches_latest_event(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_on_ticker normalizes and stores the event in _latest_ticker."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        mock_event = Mock()
        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_ticker.return_value = mock_event

        orchestrator._on_ticker("test_account", "BTCUSDT", {"topic": "tickers.BTCUSDT"})

        assert orchestrator._latest_ticker["BTCUSDT"] is mock_event

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_on_ticker_none_event_skipped(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_on_ticker skips when normalizer returns None."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_ticker.return_value = None

        orchestrator._on_ticker("test_account", "BTCUSDT", {})

        assert "BTCUSDT" not in orchestrator._latest_ticker

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_on_order_appends_to_pending_deque(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_on_order normalizes events and appends them to the runner's deque."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        # Seed pending-event deque (normally done in start())
        from collections import deque
        orchestrator._pending_orders["btcusdt_test"] = deque()

        mock_event = Mock()
        mock_event.symbol = "BTCUSDT"
        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_order.return_value = [mock_event]

        orchestrator._on_order("test_account", {"topic": "order"})

        dq = orchestrator._pending_orders["btcusdt_test"]
        assert len(dq) == 1
        assert dq[0] is mock_event

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_on_execution_appends_to_pending_deque(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_on_execution normalizes events and appends them to the runner's deque."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        from collections import deque
        orchestrator._pending_executions["btcusdt_test"] = deque()

        mock_event = Mock()
        mock_event.symbol = "BTCUSDT"
        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_execution.return_value = [mock_event]

        orchestrator._on_execution("test_account", {"topic": "execution"})

        dq = orchestrator._pending_executions["btcusdt_test"]
        assert len(dq) == 1
        assert dq[0] is mock_event


class TestOrchestratorTick:
    """Tests for the main-loop tick that drains buffers and dispatches events."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_tick_drains_executions_to_runner(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_tick drains _pending_executions into runner.on_execution."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        from collections import deque
        orchestrator._pending_executions["btcusdt_test"] = deque()
        orchestrator._pending_orders["btcusdt_test"] = deque()

        mock_runner = Mock()
        mock_runner.strat_id = "btcusdt_test"
        mock_runner.symbol = "BTCUSDT"
        orchestrator._runners = {"btcusdt_test": mock_runner}

        ev1, ev2 = Mock(), Mock()
        orchestrator._pending_executions["btcusdt_test"].extend([ev1, ev2])

        orchestrator._tick()

        assert mock_runner.on_execution.call_count == 2
        assert orchestrator._pending_executions["btcusdt_test"] == deque()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_tick_drains_orders_to_runner(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_tick drains _pending_orders into runner.on_order_update."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        from collections import deque
        orchestrator._pending_executions["btcusdt_test"] = deque()
        orchestrator._pending_orders["btcusdt_test"] = deque()

        mock_runner = Mock()
        mock_runner.strat_id = "btcusdt_test"
        mock_runner.symbol = "BTCUSDT"
        orchestrator._runners = {"btcusdt_test": mock_runner}

        ev1 = Mock()
        orchestrator._pending_orders["btcusdt_test"].append(ev1)

        orchestrator._tick()

        mock_runner.on_order_update.assert_called_once_with(ev1)
        assert orchestrator._pending_orders["btcusdt_test"] == deque()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_tick_dispatches_latest_ticker_once_per_event(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_tick dispatches each ticker at most once, coalescing older ones."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        from collections import deque
        orchestrator._pending_executions["btcusdt_test"] = deque()
        orchestrator._pending_orders["btcusdt_test"] = deque()

        mock_runner = Mock()
        mock_runner.strat_id = "btcusdt_test"
        mock_runner.symbol = "BTCUSDT"
        orchestrator._runners = {"btcusdt_test": mock_runner}
        orchestrator._symbol_to_runners = {"BTCUSDT": [mock_runner]}

        ev = Mock()
        orchestrator._latest_ticker["BTCUSDT"] = ev

        orchestrator._tick()
        orchestrator._tick()  # second tick — same event, should not re-dispatch

        mock_runner.on_ticker.assert_called_once_with(ev)

        # New event appears → dispatched on the next tick
        ev2 = Mock()
        orchestrator._latest_ticker["BTCUSDT"] = ev2
        orchestrator._tick()

        assert mock_runner.on_ticker.call_count == 2
        mock_runner.on_ticker.assert_called_with(ev2)

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_tick_exception_in_runner_does_not_crash(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """A runner raising mid-drain is logged but does not abort the tick."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        from collections import deque
        orchestrator._pending_executions["btcusdt_test"] = deque()
        orchestrator._pending_orders["btcusdt_test"] = deque()

        mock_runner = Mock()
        mock_runner.strat_id = "btcusdt_test"
        mock_runner.symbol = "BTCUSDT"
        mock_runner.on_execution.side_effect = ValueError("boom")
        orchestrator._runners = {"btcusdt_test": mock_runner}
        orchestrator._symbol_to_runners = {"BTCUSDT": [mock_runner]}

        orchestrator._pending_executions["btcusdt_test"].append(Mock())

        # Should not raise
        orchestrator._tick()

        notifier.alert_exception.assert_called_once()


class TestOrchestratorPositionCheck:
    """Tests for _fetch_and_update_positions (single-shot, called from _tick)."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_position_check_uses_ws_data(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Position check prefers WebSocket data when available."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        # Replace runner with mock
        mock_runner = Mock()
        mock_runner.strat_id = "btcusdt_test"
        mock_runner.symbol = "BTCUSDT"
        mock_runner.engine.last_close = 42000.0
        mock_runner.on_position_update = Mock()
        orchestrator._account_to_runners["test_account"] = [mock_runner]

        # Pre-populate WS position cache
        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.05"}
        orchestrator._position_ws_data = {
            "test_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}}
        }

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]
        }

        orchestrator._fetch_and_update_positions()

        mock_runner.on_position_update.assert_called_once_with(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=42000.0,
        )
        rest_client.get_positions.assert_not_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_position_check_falls_back_to_rest(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Position check falls back to REST when WS data is missing."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        mock_runner = Mock()
        mock_runner.strat_id = "btcusdt_test"
        mock_runner.symbol = "BTCUSDT"
        mock_runner.engine.last_close = 42000.0
        mock_runner.on_position_update = Mock()
        orchestrator._account_to_runners["test_account"] = [mock_runner]

        orchestrator._position_ws_data = {}

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "5000"}]}]
        }
        long_pos_rest = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.2"}
        short_pos_rest = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.1"}
        other_pos = {"symbol": "ETHUSDT", "side": "Buy", "size": "1.0"}
        rest_client.get_positions.return_value = [other_pos, long_pos_rest, short_pos_rest]

        orchestrator._fetch_and_update_positions()

        rest_client.get_positions.assert_called_once()
        mock_runner.on_position_update.assert_called_once_with(
            long_position=long_pos_rest,
            short_position=short_pos_rest,
            wallet_balance=5000.0,
            last_close=42000.0,
        )

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_position_check_handles_account_error(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Per-account errors are caught and logged, not raised."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.side_effect = Exception("API error")

        # Should not raise
        orchestrator._fetch_and_update_positions()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_position_check_total_budget_stops_extra_accounts(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Once _POSITION_FETCH_TOTAL_BUDGET is spent, subsequent accounts are deferred."""
        # Two accounts share the test config; the second must be skipped
        # once the first has burned the whole budget.
        second_account = AccountConfig(
            name="slow_account",
            api_key="k2",
            api_secret="s2",
            testnet=True,
        )

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_account(second_account)

        # Give each account its own runner so the loop iterates twice.
        runner_a = Mock(strat_id="a", symbol="BTCUSDT")
        runner_a.engine.last_close = 42000.0
        runner_b = Mock(strat_id="b", symbol="BTCUSDT")
        runner_b.engine.last_close = 42000.0
        orchestrator._account_to_runners = {
            "test_account": [runner_a],
            "slow_account": [runner_b],
        }

        # Pre-populate WS cache so only wallet_balance hits REST — keeps
        # the test path narrow and makes the budget the only variable.
        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0"}
        orchestrator._position_ws_data = {
            "test_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}},
            "slow_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}},
        }

        for name in ("test_account", "slow_account"):
            rc = orchestrator._rest_clients[name]
            rc.get_wallet_balance.return_value = {
                "list": [{"coin": [{"coin": "USDT", "walletBalance": "1000"}]}]
            }

        # Fake monotonic: the first account burns 6s (over the 5s budget)
        # between its loop entry and the post-fetch wait. Sequence:
        #   loop_start         -> 0.0
        #   1st start/gate     -> 0.0
        #   1st finally        -> 6.0   (slow fetch)
        #   2nd start/gate     -> 6.0   (budget gate trips, break)
        fake_times = iter([0.0, 0.0, 6.0, 6.0, 6.0, 6.0])
        with patch("gridbot.orchestrator.time.monotonic", side_effect=lambda: next(fake_times)):
            orchestrator._fetch_and_update_positions()

        # First account was served; second was deferred by the budget gate.
        runner_a.on_position_update.assert_called_once()
        runner_b.on_position_update.assert_not_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_position_check_startup_uses_larger_budget(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Startup pass uses the larger _STARTUP_POSITION_FETCH_BUDGET so
        a cumulative spend that would trip the steady-state budget
        still lets every account initialize."""
        second_account = AccountConfig(
            name="slow_account", api_key="k2", api_secret="s2", testnet=True,
        )

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_account(second_account)

        runner_a = Mock(strat_id="a", symbol="BTCUSDT")
        runner_a.engine.last_close = 42000.0
        runner_b = Mock(strat_id="b", symbol="BTCUSDT")
        runner_b.engine.last_close = 42000.0
        orchestrator._account_to_runners = {
            "test_account": [runner_a],
            "slow_account": [runner_b],
        }

        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0"}
        orchestrator._position_ws_data = {
            "test_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}},
            "slow_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}},
        }

        for name in ("test_account", "slow_account"):
            rc = orchestrator._rest_clients[name]
            rc.get_wallet_balance.return_value = {
                "list": [{"coin": [{"coin": "USDT", "walletBalance": "1000"}]}]
            }

        # Cumulative 20s exceeds the 5s steady-state budget but sits well
        # under the 30s startup budget — both runners still get initialized.
        fake_times = iter([0.0, 0.0, 10.0, 10.0, 20.0, 20.0])
        with patch("gridbot.orchestrator.time.monotonic", side_effect=lambda: next(fake_times)):
            orchestrator._fetch_and_update_positions(startup=True)

        runner_a.on_position_update.assert_called_once()
        runner_b.on_position_update.assert_called_once()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_position_check_startup_budget_still_bounded(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Startup is bounded: once _STARTUP_POSITION_FETCH_BUDGET is spent,
        remaining accounts are deferred to the next tick."""
        second_account = AccountConfig(
            name="slow_account", api_key="k2", api_secret="s2", testnet=True,
        )

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_account(second_account)

        runner_a = Mock(strat_id="a", symbol="BTCUSDT")
        runner_a.engine.last_close = 42000.0
        runner_b = Mock(strat_id="b", symbol="BTCUSDT")
        runner_b.engine.last_close = 42000.0
        orchestrator._account_to_runners = {
            "test_account": [runner_a],
            "slow_account": [runner_b],
        }

        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0"}
        orchestrator._position_ws_data = {
            "test_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}},
            "slow_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}},
        }

        for name in ("test_account", "slow_account"):
            rc = orchestrator._rest_clients[name]
            rc.get_wallet_balance.return_value = {
                "list": [{"coin": [{"coin": "USDT", "walletBalance": "1000"}]}]
            }

        # Cumulative 31s exceeds the 30s startup budget — second account
        # must be deferred to the next tick.
        fake_times = iter([0.0, 0.0, 31.0, 31.0, 31.0, 31.0])
        with patch("gridbot.orchestrator.time.monotonic", side_effect=lambda: next(fake_times)):
            orchestrator._fetch_and_update_positions(startup=True)

        runner_a.on_position_update.assert_called_once()
        runner_b.on_position_update.assert_not_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_position_check_floor_skips_rapid_repeat(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Steady-state: if the account was fetched < _POSITION_FETCH_MIN_INTERVAL
        ago, the next call is short-circuited (no wallet/position fetch)."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        mock_runner = Mock(strat_id="btcusdt_test", symbol="BTCUSDT")
        mock_runner.engine.last_close = 42000.0
        orchestrator._account_to_runners["test_account"] = [mock_runner]

        # Mark the account as just-fetched: loop_start=0.0, last=0.2
        # → delta 0.2s < floor 1.0s → must be skipped.
        orchestrator._last_position_fetch["test_account"] = 0.2

        fake_times = iter([0.0, 0.0])  # loop_start + per-account start
        with patch("gridbot.orchestrator.time.monotonic", side_effect=lambda: next(fake_times)):
            orchestrator._fetch_and_update_positions()

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.assert_not_called()
        rest_client.get_positions.assert_not_called()
        mock_runner.on_position_update.assert_not_called()
        # Timestamp is NOT overwritten when the call is skipped.
        assert orchestrator._last_position_fetch["test_account"] == 0.2

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_position_check_floor_bypassed_on_startup(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Startup pass ignores the floor so the initial fetch always runs,
        even if _last_position_fetch was somehow set < floor seconds ago."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        mock_runner = Mock(strat_id="btcusdt_test", symbol="BTCUSDT")
        mock_runner.engine.last_close = 42000.0
        mock_runner.on_position_update = Mock()
        orchestrator._account_to_runners["test_account"] = [mock_runner]

        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0"}
        orchestrator._position_ws_data = {
            "test_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}}
        }
        orchestrator._last_position_fetch["test_account"] = 0.2  # would skip in steady-state

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "1000"}]}]
        }

        orchestrator._fetch_and_update_positions(startup=True)

        # Floor is bypassed: the fetch proceeded and the runner got an update.
        mock_runner.on_position_update.assert_called_once()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_position_check_floor_records_last_fetch(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """After a successful steady-state fetch, _last_position_fetch is
        updated with the per-account start timestamp so the next call is
        gated by the floor."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        mock_runner = Mock(strat_id="btcusdt_test", symbol="BTCUSDT")
        mock_runner.engine.last_close = 42000.0
        mock_runner.on_position_update = Mock()
        orchestrator._account_to_runners["test_account"] = [mock_runner]

        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0"}
        orchestrator._position_ws_data = {
            "test_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}}
        }

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "1000"}]}]
        }

        assert "test_account" not in orchestrator._last_position_fetch

        # loop_start=100.0, per-account start=100.0, finally=100.1
        fake_times = iter([100.0, 100.0, 100.1])
        with patch("gridbot.orchestrator.time.monotonic", side_effect=lambda: next(fake_times)):
            orchestrator._fetch_and_update_positions()

        mock_runner.on_position_update.assert_called_once()
        assert orchestrator._last_position_fetch["test_account"] == 100.0


class TestOrchestratorDbRecords:
    """Tests for database Run record creation and update."""

    def test_create_run_records_skips_when_no_db(self, gridbot_config):
        """No error when db is None."""
        orchestrator = Orchestrator(gridbot_config, db=None)
        orchestrator._create_run_records()
        assert orchestrator._run_ids == {}

    def test_create_run_records_populates_run_ids(self, gridbot_config):
        """_create_run_records populates _run_ids keyed by strat_id."""
        from grid_db import DatabaseFactory, DatabaseSettings
        db = DatabaseFactory(DatabaseSettings(db_name=":memory:"))
        db.create_tables()

        orchestrator = Orchestrator(gridbot_config, db=db)
        orchestrator._create_run_records()

        assert "btcusdt_test" in orchestrator._run_ids
        run_id = orchestrator._run_ids["btcusdt_test"]
        assert run_id is not None

        with db.get_session() as session:
            from grid_db import Run
            run = session.get(Run, str(run_id))
            assert run is not None
            assert run.status == "running"
            assert run.run_type == "live"

    def test_create_run_records_shadow_mode(self, account_config):
        """Shadow-mode strategies create runs with run_type='shadow'."""
        from grid_db import DatabaseFactory, DatabaseSettings
        db = DatabaseFactory(DatabaseSettings(db_name=":memory:"))
        db.create_tables()

        shadow_strategy = StrategyConfig(
            strat_id="shadow_test",
            account="test_account",
            symbol="BTCUSDT",
            tick_size="0.1",
            shadow_mode=True,
        )
        config = GridbotConfig(
            accounts=[account_config],
            strategies=[shadow_strategy],
            database_url="sqlite:///:memory:",
        )

        orchestrator = Orchestrator(config, db=db)
        orchestrator._create_run_records()

        run_id = orchestrator._run_ids["shadow_test"]
        with db.get_session() as session:
            from grid_db import Run
            run = session.get(Run, str(run_id))
            assert run.run_type == "shadow"

    def test_update_run_records_marks_completed(self, gridbot_config):
        """_update_run_records_stopped sets status to 'completed'."""
        from grid_db import DatabaseFactory, DatabaseSettings
        db = DatabaseFactory(DatabaseSettings(db_name=":memory:"))
        db.create_tables()

        orchestrator = Orchestrator(gridbot_config, db=db)
        orchestrator._create_run_records()

        run_id = orchestrator._run_ids["btcusdt_test"]
        orchestrator._update_run_records_stopped()

        with db.get_session() as session:
            from grid_db import Run
            run = session.get(Run, str(run_id))
            assert run.status == "completed"
            assert run.end_ts is not None

    def test_update_run_records_skips_when_no_run_ids(self, gridbot_config):
        """No error when _run_ids is empty."""
        orchestrator = Orchestrator(gridbot_config, db=Mock())
        orchestrator._update_run_records_stopped()

    def test_create_run_records_handles_db_error(self, gridbot_config):
        """DB errors are logged as warnings, not raised."""
        db = Mock()
        db.get_session.side_effect = Exception("connection failed")

        orchestrator = Orchestrator(gridbot_config, db=db)
        orchestrator._create_run_records()
        assert orchestrator._run_ids == {}


class TestOrchestratorRetryDispatcher:
    """Tests for retry queue intent dispatch routing."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_retry_dispatcher_routes_cancel_to_execute_cancel(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Retry queue dispatches CancelIntent to execute_cancel."""
        from gridcore.intents import CancelIntent

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)

        executor = orchestrator._runners["btcusdt_test"]._executor
        retry_queue = orchestrator._retry_queues["btcusdt_test"]
        dispatcher = retry_queue._executor_func

        cancel = CancelIntent(symbol="BTCUSDT", order_id="order_123", reason="test")

        executor.execute_cancel = MagicMock(return_value=Mock(success=True))
        executor.execute_place = MagicMock(return_value=Mock(success=True))

        dispatcher(cancel)

        executor.execute_cancel.assert_called_once_with(cancel)
        executor.execute_place.assert_not_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_retry_dispatcher_routes_place_to_execute_place(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Retry queue dispatches PlaceLimitIntent to execute_place."""
        from gridcore.intents import PlaceLimitIntent

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)

        executor = orchestrator._runners["btcusdt_test"]._executor
        retry_queue = orchestrator._retry_queues["btcusdt_test"]
        dispatcher = retry_queue._executor_func

        place = PlaceLimitIntent.create(
            symbol="BTCUSDT",
            side="Buy",
            price=Decimal("50000.0"),
            qty=Decimal("0.001"),
            grid_level=10,
            direction="long",
        )

        executor.execute_cancel = MagicMock(return_value=Mock(success=True))
        executor.execute_place = MagicMock(return_value=Mock(success=True))

        dispatcher(place)

        executor.execute_place.assert_called_once_with(place)
        executor.execute_cancel.assert_not_called()


class TestOrchestratorExceptionHandling:
    """Tests for exception handling in WebSocket callbacks."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_on_ticker_exception_does_not_crash(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_on_ticker catches exceptions and notifies."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_ticker.side_effect = ValueError("bad data")

        orchestrator._on_ticker("test_account", "BTCUSDT", {})

        notifier.alert_exception.assert_called_once()
        assert "on_ticker" in notifier.alert_exception.call_args[0][0]

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_on_order_exception_does_not_crash(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_on_order catches exceptions and notifies."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_order.side_effect = KeyError("missing")

        orchestrator._on_order("test_account", {})

        notifier.alert_exception.assert_called_once()
        assert "on_order" in notifier.alert_exception.call_args[0][0]

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_on_execution_exception_does_not_crash(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_on_execution catches exceptions and notifies."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_execution.side_effect = TypeError("oops")

        orchestrator._on_execution("test_account", {})

        notifier.alert_exception.assert_called_once()
        assert "on_execution" in notifier.alert_exception.call_args[0][0]

    def test_on_position_exception_does_not_crash(self, gridbot_config):
        """_on_position catches broad exceptions and notifies."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)

        orchestrator._on_position("test_account", {"data": 12345})

        notifier.alert_exception.assert_called_once()
        assert "on_position" in notifier.alert_exception.call_args[0][0]


class TestOrchestratorHealthCheckOnce:
    """Tests for _health_check_once (single-shot, called from _tick)."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_health_check_reconnects_disconnected_public_ws(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Health check reconnects a disconnected public WebSocket."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = False
        pub_ws.connect = Mock()
        pub_ws.disconnect = Mock()

        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = True

        orchestrator._health_check_once()

        pub_ws.disconnect.assert_called_once()
        pub_ws.connect.assert_called_once()
        notifier.alert.assert_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_health_check_reconnects_disconnected_private_ws(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Health check reconnects a disconnected private WebSocket."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = True

        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = False
        priv_ws.connect = Mock()
        priv_ws.disconnect = Mock()

        orchestrator._health_check_once()

        priv_ws.disconnect.assert_called_once()
        priv_ws.connect.assert_called_once()
        notifier.alert.assert_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_health_check_skips_connected(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Health check does nothing when all WS are connected."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = True

        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = True

        orchestrator._health_check_once()

        notifier.alert.assert_not_called()
        notifier.alert_exception.assert_not_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_health_check_reconnect_failure_notifies(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Health check notifies on reconnect failure."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = False
        pub_ws.disconnect = Mock()
        pub_ws.connect = Mock(side_effect=Exception("connection refused"))

        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = True

        orchestrator._health_check_once()

        assert notifier.alert.call_count >= 1
        assert notifier.alert_exception.call_count >= 1


class TestOrchestratorOrderSyncOnce:
    """Tests for _order_sync_once (single-shot, called from _tick)."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_order_sync_once_calls_reconcile_reconnect(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_order_sync_once calls reconcile_reconnect for each runner."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        reconciler = orchestrator._reconcilers["test_account"]
        reconciler.reconcile_reconnect = Mock(
            return_value=ReconciliationResult(
                orders_fetched=5,
                orders_injected=0,
                untracked_orders_on_exchange=0,
            )
        )

        orchestrator._order_sync_once()

        reconciler.reconcile_reconnect.assert_called_once()
        runner = orchestrator._runners["btcusdt_test"]
        reconciler.reconcile_reconnect.assert_called_with(runner)

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_tick_skips_order_sync_when_interval_zero(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        account_config, strategy_config,
    ):
        """_tick never calls _order_sync_once when order_sync_interval <= 0."""
        config = GridbotConfig(
            accounts=[account_config],
            strategies=[strategy_config],
            order_sync_interval=0.0,
        )
        orchestrator = Orchestrator(config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        from collections import deque
        orchestrator._pending_executions["btcusdt_test"] = deque()
        orchestrator._pending_orders["btcusdt_test"] = deque()

        reconciler = orchestrator._reconcilers["test_account"]
        reconciler.reconcile_reconnect = Mock()

        orchestrator._tick()

        reconciler.reconcile_reconnect.assert_not_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_order_sync_once_handles_errors(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Per-runner errors in order sync are logged, not raised."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        reconciler = orchestrator._reconcilers["test_account"]
        reconciler.reconcile_reconnect = Mock(side_effect=Exception("API error"))

        # Should not raise
        orchestrator._order_sync_once()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_order_sync_once_logs_discrepancies(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Discrepancies are reported via logger."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        reconciler = orchestrator._reconcilers["test_account"]
        reconciler.reconcile_reconnect = Mock(
            return_value=ReconciliationResult(
                orders_fetched=10,
                orders_injected=2,
                untracked_orders_on_exchange=1,
            )
        )

        orchestrator._order_sync_once()

        reconciler.reconcile_reconnect.assert_called_once()


class TestOrchestratorWalletCache:
    """Tests for wallet balance caching."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_get_wallet_balance_caches_result(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config,
    ):
        """Wallet balance is cached on first fetch."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "5000"}]}]
        }

        balance = orchestrator._get_wallet_balance("test_account")
        assert balance == 5000.0
        rest_client.get_wallet_balance.assert_called_once()

        assert "test_account" in orchestrator._wallet_cache
        cached_balance, _ = orchestrator._wallet_cache["test_account"]
        assert cached_balance == 5000.0

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_get_wallet_balance_returns_cached_value(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config,
    ):
        """Subsequent calls return cached value within the TTL window."""
        from datetime import datetime, UTC

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)

        orchestrator._wallet_cache["test_account"] = (10000.0, datetime.now(UTC))

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "9999"}]}]
        }

        balance = orchestrator._get_wallet_balance("test_account")
        assert balance == 10000.0
        rest_client.get_wallet_balance.assert_not_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_get_wallet_balance_refreshes_after_expiry(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config,
    ):
        """Cache expires and refetches after interval."""
        from datetime import datetime, UTC, timedelta

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)

        old_timestamp = datetime.now(UTC) - timedelta(seconds=400)
        orchestrator._wallet_cache["test_account"] = (5000.0, old_timestamp)

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "7500"}]}]
        }

        balance = orchestrator._get_wallet_balance("test_account")
        assert balance == 7500.0
        rest_client.get_wallet_balance.assert_called_once()

        cached_balance, _ = orchestrator._wallet_cache["test_account"]
        assert cached_balance == 7500.0

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_get_wallet_balance_disabled_when_interval_zero(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        account_config, strategy_config,
    ):
        """Caching is disabled when wallet_cache_interval is 0."""
        from datetime import datetime, UTC

        config = GridbotConfig(
            accounts=[account_config],
            strategies=[strategy_config],
            wallet_cache_interval=0.0,
        )
        orchestrator = Orchestrator(config)
        orchestrator._init_account(account_config)

        orchestrator._wallet_cache["test_account"] = (5000.0, datetime.now(UTC))

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "8000"}]}]
        }

        balance = orchestrator._get_wallet_balance("test_account")
        assert balance == 8000.0
        rest_client.get_wallet_balance.assert_called_once()

        rest_client.get_wallet_balance.reset_mock()
        balance = orchestrator._get_wallet_balance("test_account")
        assert balance == 8000.0
        rest_client.get_wallet_balance.assert_called_once()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_get_wallet_balance_fetch_failure_propagates(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config,
    ):
        """REST failure propagates out — no stale zero cached."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.side_effect = ConnectionError("timeout")

        with pytest.raises(ConnectionError, match="timeout"):
            orchestrator._get_wallet_balance("test_account")

        assert "test_account" not in orchestrator._wallet_cache


class TestOrchestratorAuthCooldown:
    """Tests for auth error cooldown lifecycle in orchestrator."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_cooldown_entered_sets_timer_and_alerts(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_on_auth_cooldown_entered sets expiry timer and sends alert."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)

        orchestrator._on_auth_cooldown_entered("btcusdt_test")

        assert "btcusdt_test" in orchestrator._auth_cooldown_until
        assert orchestrator._auth_cooldown_cycles["btcusdt_test"] == 1
        notifier.alert.assert_called_once()
        assert "cycle 1" in notifier.alert.call_args[0][0]

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_cooldown_cycle_increments(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Cycle count increments across cooldown entries."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)

        orchestrator._on_auth_cooldown_entered("btcusdt_test")
        assert orchestrator._auth_cooldown_cycles["btcusdt_test"] == 1

        del orchestrator._auth_cooldown_until["btcusdt_test"]

        orchestrator._on_auth_cooldown_entered("btcusdt_test")
        assert orchestrator._auth_cooldown_cycles["btcusdt_test"] == 2
        assert "cycle 2" in notifier.alert.call_args[0][0]

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_health_check_expires_cooldown(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_health_check_once resets executor when cooldown expires."""
        from datetime import datetime, timedelta, UTC

        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        executor = orchestrator._strategy_executors["btcusdt_test"]
        executor._auth_cooldown = True
        executor._auth_failure_count = 5
        orchestrator._auth_cooldown_until["btcusdt_test"] = datetime.now(UTC) - timedelta(seconds=1)
        orchestrator._auth_cooldown_cycles["btcusdt_test"] = 2

        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = True
        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = True

        orchestrator._health_check_once()

        assert executor.auth_cooldown is False
        assert executor.auth_failure_count == 0
        assert "btcusdt_test" not in orchestrator._auth_cooldown_until
        assert orchestrator._auth_cooldown_cycles["btcusdt_test"] == 2
        assert any("cooldown expired" in str(c) for c in notifier.alert.call_args_list)

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_cooldown_uses_config_minutes(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        account_config, strategy_config,
    ):
        """Cooldown timer uses auth_cooldown_minutes from config."""
        from datetime import datetime, timedelta, UTC

        config = GridbotConfig(
            accounts=[account_config],
            strategies=[strategy_config],
            auth_cooldown_minutes=10,
        )
        orchestrator = Orchestrator(config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)

        before = datetime.now(UTC)
        orchestrator._on_auth_cooldown_entered("btcusdt_test")
        after = datetime.now(UTC)

        expiry = orchestrator._auth_cooldown_until["btcusdt_test"]
        assert expiry >= before + timedelta(minutes=10)
        assert expiry <= after + timedelta(minutes=10)

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_cooldown_clears_retry_queue(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Retry queue is cleared when cooldown activates."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)

        retry_queue = orchestrator._retry_queues["btcusdt_test"]
        from gridcore.intents import PlaceLimitIntent
        intent = PlaceLimitIntent.create(
            symbol="BTCUSDT", side="Buy", price=Decimal("50000"),
            qty=Decimal("0.001"), grid_level=1, direction="long",
        )
        retry_queue.add(intent, "auth error")
        retry_queue.add(intent, "auth error")
        assert retry_queue.size == 2

        orchestrator._on_auth_cooldown_entered("btcusdt_test")

        assert retry_queue.size == 0


class TestFetchInstrumentInfo:
    """Tests for _fetch_instrument_info."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_fetch_success(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config,
    ):
        """Successful fetch returns InstrumentInfo."""
        rest_client = mock_rest_client.return_value
        rest_client.get_instruments_info.return_value = {
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "100"},
            "priceFilter": {"tickSize": "0.1"},
        }

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)

        info = orchestrator._fetch_instrument_info("BTCUSDT", "test_account")
        assert info is not None
        assert info.qty_step == Decimal("0.001")
        assert info.tick_size == Decimal("0.1")
        rest_client.get_instruments_info.assert_called_once_with("BTCUSDT")

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_fetch_api_error_returns_none(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config,
    ):
        """API exception returns None gracefully."""
        rest_client = mock_rest_client.return_value
        rest_client.get_instruments_info.side_effect = Exception("API error")

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)

        info = orchestrator._fetch_instrument_info("BTCUSDT", "test_account")
        assert info is None

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_fetch_invalid_params_returns_none(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config,
    ):
        """Zero qty_step in response returns None."""
        rest_client = mock_rest_client.return_value
        rest_client.get_instruments_info.return_value = {
            "lotSizeFilter": {"qtyStep": "0", "minOrderQty": "0.001", "maxOrderQty": "100"},
            "priceFilter": {"tickSize": "0.1"},
        }

        orchestrator = Orchestrator(gridbot_config)
        orchestrator._init_account(account_config)

        info = orchestrator._fetch_instrument_info("BTCUSDT", "test_account")
        assert info is None


class TestOrchestratorRunBackoff:
    """run() exponential backoff on consecutive _tick() failures."""

    @staticmethod
    def _make_orchestrator(gridbot_config):
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._running = True
        return orchestrator

    @staticmethod
    def _install_sleep_stub(orchestrator, stop_after):
        """Patch time.sleep to record args and stop the loop after N calls."""
        recorded: list[float] = []

        def fake_sleep(seconds):
            recorded.append(seconds)
            if len(recorded) >= stop_after:
                orchestrator._running = False

        return recorded, fake_sleep

    def test_success_sleeps_check_interval(self, gridbot_config):
        from gridbot.orchestrator import _CHECK_INTERVAL

        orchestrator = self._make_orchestrator(gridbot_config)
        orchestrator._tick = Mock(return_value=None)
        recorded, fake_sleep = self._install_sleep_stub(orchestrator, stop_after=1)

        with patch("gridbot.orchestrator.time.sleep", side_effect=fake_sleep):
            orchestrator.run()

        assert recorded == [_CHECK_INTERVAL]

    def test_escalates_1_2_4_on_repeated_failures(self, gridbot_config):
        orchestrator = self._make_orchestrator(gridbot_config)
        orchestrator._tick = Mock(side_effect=RuntimeError("boom"))
        recorded, fake_sleep = self._install_sleep_stub(orchestrator, stop_after=3)

        with patch("gridbot.orchestrator.time.sleep", side_effect=fake_sleep):
            orchestrator.run()

        assert recorded == [1.0, 2.0, 4.0]
        assert orchestrator._notifier.alert_exception.call_count == 3

    def test_resets_to_check_interval_after_success(self, gridbot_config):
        from gridbot.orchestrator import _CHECK_INTERVAL

        orchestrator = self._make_orchestrator(gridbot_config)
        orchestrator._tick = Mock(
            side_effect=[RuntimeError("a"), RuntimeError("b"), None]
        )
        recorded, fake_sleep = self._install_sleep_stub(orchestrator, stop_after=3)

        with patch("gridbot.orchestrator.time.sleep", side_effect=fake_sleep):
            orchestrator.run()

        assert recorded == [1.0, 2.0, _CHECK_INTERVAL]

    def test_caps_at_max_tick_backoff(self, gridbot_config):
        from gridbot.orchestrator import _MAX_TICK_BACKOFF

        orchestrator = self._make_orchestrator(gridbot_config)
        orchestrator._tick = Mock(side_effect=RuntimeError("down"))
        recorded, fake_sleep = self._install_sleep_stub(orchestrator, stop_after=10)

        with patch("gridbot.orchestrator.time.sleep", side_effect=fake_sleep):
            orchestrator.run()

        assert len(recorded) == 10
        assert all(s <= _MAX_TICK_BACKOFF for s in recorded)
        # Progression: 1, 2, 4, 8, 16, 32, 64, 128, 180 (cap), 180
        assert recorded == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 180.0, 180.0]


class TestOrchestratorRunRequestStop:
    """End-to-end threading contract: run() exits promptly when request_stop()
    is called from another thread."""

    def test_run_exits_on_request_stop_from_another_thread(self, gridbot_config):
        orchestrator = Orchestrator(gridbot_config, notifier=Mock(spec=Notifier))

        tick_count = {"n": 0}

        def counting_tick():
            tick_count["n"] += 1

        orchestrator._tick = Mock(side_effect=counting_tick)
        orchestrator._running = True

        thread = threading.Thread(target=orchestrator.run, name="run-loop")
        thread.start()

        # Give the loop time to run a few ticks (_CHECK_INTERVAL=0.1s).
        deadline = time.monotonic() + 1.0
        while tick_count["n"] < 3 and time.monotonic() < deadline:
            time.sleep(0.05)

        assert tick_count["n"] >= 3, "run() didn't tick — loop never started"

        # Cross-thread stop signal.
        orchestrator.request_stop()

        # Worst case: one tick already in-flight + one _CHECK_INTERVAL sleep.
        # 2s leaves plenty of margin on any CI.
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "run() did not exit within 2s of request_stop()"
        assert orchestrator._running is False
