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
            orchestrator._position_fetcher, "fetch_and_update"
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
        orchestrator._position_fetcher.get_wallet_balance = Mock(
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
        """on_position_message stores linear position data in cache."""
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
        orchestrator._position_fetcher.on_position_message("test_account", message)

        cached = orchestrator._position_fetcher._position_ws_data["test_account"]["BTCUSDT"]["Buy"]
        assert cached["size"] == "0.1"
        assert cached["avgPrice"] == "42500.00"

    def test_on_position_stores_both_sides(self, gridbot_config):
        """on_position_message stores both Buy and Sell positions."""
        orchestrator = Orchestrator(gridbot_config)
        message = {
            "data": [
                {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "size": "0.1"},
                {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "size": "0.05"},
            ]
        }
        orchestrator._position_fetcher.on_position_message("test_account", message)

        assert orchestrator._position_fetcher._position_ws_data["test_account"]["BTCUSDT"]["Buy"]["size"] == "0.1"
        assert orchestrator._position_fetcher._position_ws_data["test_account"]["BTCUSDT"]["Sell"]["size"] == "0.05"

    def test_on_position_filters_non_linear(self, gridbot_config):
        """on_position_message ignores non-linear positions."""
        orchestrator = Orchestrator(gridbot_config)
        message = {
            "data": [
                {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "size": "1.0"},
            ]
        }
        orchestrator._position_fetcher.on_position_message("test_account", message)
        assert len(orchestrator._position_fetcher._position_ws_data.get("test_account", {})) == 0

    def test_on_position_skips_empty_symbol_or_side(self, gridbot_config):
        """on_position_message skips entries with empty symbol or side."""
        orchestrator = Orchestrator(gridbot_config)
        message = {
            "data": [
                {"category": "linear", "symbol": "", "side": "Buy", "size": "0.1"},
                {"category": "linear", "symbol": "BTCUSDT", "side": "", "size": "0.1"},
            ]
        }
        orchestrator._position_fetcher.on_position_message("test_account", message)
        account_data = orchestrator._position_fetcher._position_ws_data.get("test_account", {})
        assert len(account_data.get("BTCUSDT", {})) == 0

    def test_on_position_handles_empty_data(self, gridbot_config):
        """on_position_message handles empty or missing data gracefully."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._position_fetcher.on_position_message("test_account", {"data": []})
        orchestrator._position_fetcher.on_position_message("test_account", {})

    def test_get_position_from_ws_returns_cached_data(self, gridbot_config):
        """get_position_from_ws returns data when available."""
        orchestrator = Orchestrator(gridbot_config)
        pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"}
        orchestrator._position_fetcher._position_ws_data = {"acct": {"BTCUSDT": {"Buy": pos}}}

        result = orchestrator._position_fetcher.get_position_from_ws("acct", "BTCUSDT", "Buy")
        assert result == pos

    def test_get_position_from_ws_returns_none_when_missing(self, gridbot_config):
        """get_position_from_ws returns None for missing data."""
        orchestrator = Orchestrator(gridbot_config)
        assert orchestrator._position_fetcher.get_position_from_ws("missing", "BTCUSDT", "Buy") is None

        orchestrator._position_fetcher._position_ws_data = {"acct": {}}
        assert orchestrator._position_fetcher.get_position_from_ws("acct", "BTCUSDT", "Buy") is None

        orchestrator._position_fetcher._position_ws_data = {"acct": {"BTCUSDT": {}}}
        assert orchestrator._position_fetcher.get_position_from_ws("acct", "BTCUSDT", "Buy") is None


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


class TestOrchestratorTickPeriodicCheckIsolation:
    """Each periodic REST check is isolated so one failure doesn't wedge
    WS-event drain via the outer backoff path."""

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_failing_position_check_does_not_escape_tick(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_tick() swallows _fetch_and_update_positions errors and advances its timer."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        orchestrator._position_fetcher.fetch_and_update = Mock(side_effect=RuntimeError("boom"))
        orchestrator._next_position_check = 0.0  # due now

        before = orchestrator._next_position_check
        orchestrator._tick()  # must not raise

        assert orchestrator._next_position_check > before, (
            "_next_position_check must advance even when the check raises"
        )
        notifier.alert_exception.assert_any_call(
            "_fetch_and_update_positions",
            orchestrator._position_fetcher.fetch_and_update.side_effect,
            error_key="periodic_fetch_positions",
        )

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_failing_health_check_does_not_starve_ws_drain(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """A raising _health_check_once does not prevent ticker dispatch."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        mock_runner = Mock()
        mock_runner.strat_id = "btcusdt_test"
        mock_runner.symbol = "BTCUSDT"
        orchestrator._runners = {"btcusdt_test": mock_runner}
        orchestrator._symbol_to_runners = {"BTCUSDT": [mock_runner]}

        orchestrator._health_check_once = Mock(side_effect=RuntimeError("boom"))
        orchestrator._next_health_check = 0.0  # due now

        ticker_event = Mock()
        orchestrator._latest_ticker["BTCUSDT"] = ticker_event

        orchestrator._tick()

        mock_runner.on_ticker.assert_called_once_with(ticker_event)
        notifier.alert_exception.assert_any_call(
            "_health_check_once",
            orchestrator._health_check_once.side_effect,
            error_key="periodic_health_check",
        )

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_failing_order_sync_alerts_with_stable_key(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Repeated _order_sync_once failures alert with the same throttle key."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        orchestrator._order_sync_once = Mock(side_effect=RuntimeError("boom"))

        # Ensure order sync is enabled and due.
        orchestrator._config.order_sync_interval = 0.01
        orchestrator._next_order_sync = 0.0
        orchestrator._tick()
        orchestrator._next_order_sync = 0.0
        orchestrator._tick()

        order_sync_calls = [
            c for c in notifier.alert_exception.call_args_list
            if c.kwargs.get("error_key") == "periodic_order_sync"
        ]
        assert len(order_sync_calls) == 2

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_persistently_failing_check_retries_at_interval_not_every_tick(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """A failing periodic check advances its timestamp so it doesn't fire every tick."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        orchestrator._position_fetcher.fetch_and_update = Mock(side_effect=RuntimeError("boom"))

        # First tick: due now, should fire once and advance.
        orchestrator._next_position_check = 0.0
        orchestrator._tick()
        assert orchestrator._position_fetcher.fetch_and_update.call_count == 1

        # Second tick immediately after: not yet due (timestamp advanced), must not fire.
        orchestrator._tick()
        assert orchestrator._position_fetcher.fetch_and_update.call_count == 1


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
        orchestrator._position_fetcher._position_ws_data = {
            "test_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}}
        }

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]
        }

        orchestrator._position_fetcher.fetch_and_update()

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

        orchestrator._position_fetcher._position_ws_data = {}

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "5000"}]}]
        }
        long_pos_rest = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.2"}
        short_pos_rest = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.1"}
        other_pos = {"symbol": "ETHUSDT", "side": "Buy", "size": "1.0"}
        rest_client.get_positions.return_value = [other_pos, long_pos_rest, short_pos_rest]

        orchestrator._position_fetcher.fetch_and_update()

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
        orchestrator._position_fetcher.fetch_and_update()


class TestOrchestratorPositionCheckStartupBatch:
    """Startup pass: sequential fetch of every account, hard 60s cap."""

    def _make_orch_with_accounts(
        self, gridbot_config, mock_rest_factory, n: int,
    ):
        """Build an orchestrator with N accounts, each with one mock runner.

        Mutates `_account_to_runners` / `_rest_clients` in place (via
        `.clear()` + key assignment) so the PositionFetcher that was
        constructed in Orchestrator.__init__ keeps seeing the same
        dict objects it was handed.
        """
        orchestrator = Orchestrator(gridbot_config, notifier=Mock(spec=Notifier))
        orchestrator._account_to_runners.clear()
        orchestrator._rest_clients.clear()
        runners = {}
        for i in range(n):
            name = f"acct_{i}"
            runner = Mock(strat_id=f"s_{i}", symbol="BTCUSDT")
            runner.engine.last_close = 42000.0
            runner.on_position_update = Mock()
            orchestrator._account_to_runners[name] = [runner]
            orchestrator._rest_clients[name] = Mock()
            runners[name] = runner
        return orchestrator, runners

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_startup_fetches_all_accounts_within_cap(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config,
    ):
        orchestrator, runners = self._make_orch_with_accounts(gridbot_config, mock_rest_client, 4)
        fetcher = orchestrator._position_fetcher
        fetcher._fetch_one_account = Mock()

        fetcher._fetch_positions_startup_batch()

        assert fetcher._fetch_one_account.call_count == 4
        # All 4 accounts recorded a last-fetch timestamp.
        assert set(fetcher._last_position_fetch.keys()) == set(runners.keys())
        # Rotation index reset to 0 for subsequent steady-state.
        assert fetcher._position_fetch_rotation_index == 0

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_startup_raises_on_hard_cap_exceeded(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config,
    ):
        from gridbot.position_fetcher import StartupTimeoutError, _POSITION_STARTUP_HARD_CAP
        orchestrator, runners = self._make_orch_with_accounts(gridbot_config, mock_rest_client, 3)
        fetcher = orchestrator._position_fetcher
        fetcher._fetch_one_account = Mock()

        # Fake monotonic: 0, 0, cap+1 → first account fetched, second tripped.
        fake_times = iter([0.0, 0.0, _POSITION_STARTUP_HARD_CAP + 1.0,
                           _POSITION_STARTUP_HARD_CAP + 1.0, _POSITION_STARTUP_HARD_CAP + 1.0])
        with patch("gridbot.position_fetcher.time.monotonic", side_effect=lambda: next(fake_times)):
            with pytest.raises(StartupTimeoutError) as exc_info:
                fetcher._fetch_positions_startup_batch()

        assert "1/3 accounts" in str(exc_info.value)
        assert fetcher._fetch_one_account.call_count == 1

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_startup_exception_in_one_account_does_not_abort(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config,
    ):
        orchestrator, runners = self._make_orch_with_accounts(gridbot_config, mock_rest_client, 3)
        fetcher = orchestrator._position_fetcher

        calls: list[str] = []

        def fake_fetch_one(account_name, runner_list):
            calls.append(account_name)
            if account_name == "acct_1":
                raise RuntimeError("boom")

        fetcher._fetch_one_account = fake_fetch_one
        fetcher._fetch_positions_startup_batch()

        # All three attempted despite acct_1 raising.
        assert calls == ["acct_0", "acct_1", "acct_2"]


class TestOrchestratorPositionCheckRotation:
    """Steady-state: one account per tick, round-robin with per-account floor."""

    def _make_orch_with_accounts(self, gridbot_config, n: int):
        orchestrator = Orchestrator(gridbot_config, notifier=Mock(spec=Notifier))
        orchestrator._account_to_runners.clear()
        orchestrator._rest_clients.clear()
        for i in range(n):
            name = f"acct_{i}"
            runner = Mock(strat_id=f"s_{i}", symbol="BTCUSDT")
            runner.engine.last_close = 42000.0
            orchestrator._account_to_runners[name] = [runner]
            orchestrator._rest_clients[name] = Mock()
        return orchestrator

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_rotation_fetches_one_account_per_tick(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config,
    ):
        orchestrator = self._make_orch_with_accounts(gridbot_config, 4)
        fetcher = orchestrator._position_fetcher
        fetcher._fetch_one_account = Mock()

        # All floors already satisfied: pretend each account was last fetched
        # a long time ago so every one is eligible.
        for name in orchestrator._account_to_runners:
            fetcher._last_position_fetch[name] = 0.0

        calls: list[str] = []
        fetcher._fetch_one_account.side_effect = lambda n, r: calls.append(n)

        with patch("gridbot.position_fetcher.time.monotonic", return_value=10_000.0):
            for _ in range(4):
                fetcher._fetch_positions_rotation_tick()

        assert calls == ["acct_0", "acct_1", "acct_2", "acct_3"]
        assert fetcher._position_fetch_rotation_index == 0  # wrapped

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_rotation_guarantees_all_accounts_reached(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config,
    ):
        """After N ticks every account must have been fetched at least once."""
        orchestrator = self._make_orch_with_accounts(gridbot_config, 5)
        fetcher = orchestrator._position_fetcher
        fetcher._fetch_one_account = Mock()
        for name in orchestrator._account_to_runners:
            fetcher._last_position_fetch[name] = 0.0

        visited: list[str] = []
        fetcher._fetch_one_account.side_effect = lambda n, r: visited.append(n)

        with patch("gridbot.position_fetcher.time.monotonic", return_value=10_000.0):
            for _ in range(5):
                fetcher._fetch_positions_rotation_tick()

        assert set(visited) == set(orchestrator._account_to_runners.keys())

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_per_account_floor_skips_recently_fetched(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config,
    ):
        """Account just fetched must be skipped; rotation advances to next eligible."""
        orchestrator = self._make_orch_with_accounts(gridbot_config, 3)
        fetcher = orchestrator._position_fetcher
        fetcher._fetch_one_account = Mock()

        # acct_0 recently fetched; acct_1 and acct_2 very old.
        fetcher._last_position_fetch["acct_0"] = 9_999.5  # ~0.5s ago
        fetcher._last_position_fetch["acct_1"] = 0.0
        fetcher._last_position_fetch["acct_2"] = 0.0
        fetcher._position_fetch_rotation_index = 0

        calls: list[str] = []
        fetcher._fetch_one_account.side_effect = lambda n, r: calls.append(n)

        with patch("gridbot.position_fetcher.time.monotonic", return_value=10_000.0):
            fetcher._fetch_positions_rotation_tick()

        # acct_0 was skipped (still within floor), acct_1 was the first eligible.
        assert calls == ["acct_1"]
        assert fetcher._position_fetch_rotation_index == 2  # next after acct_1

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_per_account_floor_scales_with_N(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config,
    ):
        """Floor = max(config.position_check_interval, N * _POSITION_TICK_BASE).
        With N=10 and _POSITION_TICK_BASE=15s, floor must be 150s (not 63s).
        """
        from gridbot.position_fetcher import _POSITION_TICK_BASE
        orchestrator = self._make_orch_with_accounts(gridbot_config, 10)
        fetcher = orchestrator._position_fetcher
        fetcher._fetch_one_account = Mock()

        # All accounts fetched 100s ago: less than 10 * 15 = 150s floor,
        # so the rotation tick must find nobody eligible and do nothing.
        for name in orchestrator._account_to_runners:
            fetcher._last_position_fetch[name] = 9_900.0  # 100s before now

        with patch("gridbot.position_fetcher.time.monotonic", return_value=10_000.0):
            fetcher._fetch_positions_rotation_tick()

        fetcher._fetch_one_account.assert_not_called()

        # Now bump clock to >150s ago → eligible again.
        for name in orchestrator._account_to_runners:
            fetcher._last_position_fetch[name] = 9_800.0  # 200s before now
        with patch("gridbot.position_fetcher.time.monotonic", return_value=10_000.0):
            fetcher._fetch_positions_rotation_tick()

        assert fetcher._fetch_one_account.call_count == 1
        # Confirm floor value for clarity.
        assert max(float(orchestrator._config.position_check_interval),
                   10 * _POSITION_TICK_BASE) == 150.0

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_rotation_noop_when_no_accounts_eligible(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config,
    ):
        orchestrator = self._make_orch_with_accounts(gridbot_config, 2)
        fetcher = orchestrator._position_fetcher
        fetcher._fetch_one_account = Mock()

        # Both accounts just fetched; nobody eligible.
        now = 10_000.0
        for name in orchestrator._account_to_runners:
            fetcher._last_position_fetch[name] = now - 0.1

        with patch("gridbot.position_fetcher.time.monotonic", return_value=now):
            fetcher._fetch_positions_rotation_tick()

        fetcher._fetch_one_account.assert_not_called()

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_tick_advances_next_position_check_by_tick_base(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """_tick sets _next_position_check = now + _POSITION_TICK_BASE (not
        the larger config.position_check_interval)."""
        from gridbot.orchestrator import _POSITION_TICK_BASE
        orchestrator = Orchestrator(gridbot_config, notifier=Mock(spec=Notifier))
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        orchestrator._position_fetcher.fetch_and_update = Mock()
        orchestrator._next_position_check = 0.0  # due immediately

        with patch("gridbot.orchestrator.time.monotonic", return_value=1_000.0):
            orchestrator._tick()

        assert orchestrator._next_position_check == 1_000.0 + _POSITION_TICK_BASE


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
        """on_position_message catches broad exceptions and notifies."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)

        orchestrator._position_fetcher.on_position_message("test_account", {"data": 12345})

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

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_health_check_warns_on_slow_reconnect(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config, caplog,
    ):
        """A reconnect exceeding the threshold emits a WARNING."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        # Public branch enters reconnect and consumes both time.monotonic calls
        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = False
        pub_ws.connect = Mock()
        pub_ws.disconnect = Mock()

        # Private branch skipped so the iterator is not drained further
        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = True

        # Cooldown sweep at top of _health_check_once is a no-op
        orchestrator._auth_cooldown_until = {}

        with patch(
            "gridbot.orchestrator.time.monotonic",
            side_effect=iter([0.0, 6.0]),
        ):
            with caplog.at_level("WARNING", logger="gridbot.orchestrator"):
                orchestrator._health_check_once()

        matching = [
            r for r in caplog.records
            if r.levelname == "WARNING"
            and "Public WS reconnect" in r.getMessage()
            and "took" in r.getMessage()
            and "blocking main polling loop" in r.getMessage()
        ]
        assert len(matching) == 1, (
            f"Expected one slow-reconnect WARNING, got {len(matching)}: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_health_check_warns_on_slow_private_reconnect(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config, caplog,
    ):
        """Private-branch slow reconnect emits its own distinct WARNING."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        orchestrator._init_account(account_config)
        orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        # Public branch skipped so the monotonic iterator is only consumed
        # by the private branch (start + finally = exactly 2 calls).
        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = True

        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = False
        priv_ws.connect = Mock()
        priv_ws.disconnect = Mock()

        orchestrator._auth_cooldown_until = {}

        with patch(
            "gridbot.orchestrator.time.monotonic",
            side_effect=iter([0.0, 6.0]),
        ):
            with caplog.at_level("WARNING", logger="gridbot.orchestrator"):
                orchestrator._health_check_once()

        matching = [
            r for r in caplog.records
            if r.levelname == "WARNING"
            and "Private WS reconnect" in r.getMessage()
            and "took" in r.getMessage()
            and "blocking main polling loop" in r.getMessage()
        ]
        assert len(matching) == 1, (
            f"Expected one slow-reconnect WARNING, got {len(matching)}: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    def test_health_check_no_warn_on_fast_reconnect(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config, caplog,
    ):
        """A reconnect under the threshold must NOT emit the warning."""
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

        orchestrator._auth_cooldown_until = {}

        with patch(
            "gridbot.orchestrator.time.monotonic",
            side_effect=iter([0.0, 0.5]),
        ):
            with caplog.at_level("WARNING", logger="gridbot.orchestrator"):
                orchestrator._health_check_once()

        matching = [
            r for r in caplog.records
            if r.levelname == "WARNING"
            and "WS reconnect" in r.getMessage()
            and "blocking main polling loop" in r.getMessage()
        ]
        assert matching == [], (
            f"Unexpected slow-reconnect WARNING on fast path: "
            f"{[r.getMessage() for r in matching]}"
        )


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

        balance = orchestrator._position_fetcher.get_wallet_balance("test_account")
        assert balance == 5000.0
        rest_client.get_wallet_balance.assert_called_once()

        assert "test_account" in orchestrator._position_fetcher._wallet_cache
        cached_balance, _ = orchestrator._position_fetcher._wallet_cache["test_account"]
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

        orchestrator._position_fetcher._wallet_cache["test_account"] = (10000.0, datetime.now(UTC))

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "9999"}]}]
        }

        balance = orchestrator._position_fetcher.get_wallet_balance("test_account")
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
        orchestrator._position_fetcher._wallet_cache["test_account"] = (5000.0, old_timestamp)

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "7500"}]}]
        }

        balance = orchestrator._position_fetcher.get_wallet_balance("test_account")
        assert balance == 7500.0
        rest_client.get_wallet_balance.assert_called_once()

        cached_balance, _ = orchestrator._position_fetcher._wallet_cache["test_account"]
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

        orchestrator._position_fetcher._wallet_cache["test_account"] = (5000.0, datetime.now(UTC))

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "8000"}]}]
        }

        balance = orchestrator._position_fetcher.get_wallet_balance("test_account")
        assert balance == 8000.0
        rest_client.get_wallet_balance.assert_called_once()

        rest_client.get_wallet_balance.reset_mock()
        balance = orchestrator._position_fetcher.get_wallet_balance("test_account")
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
            orchestrator._position_fetcher.get_wallet_balance("test_account")

        assert "test_account" not in orchestrator._position_fetcher._wallet_cache


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
        # Progression: 1, 2, 4, 8, 16, 30 (cap hit at n=6 since 32 > 30), then 30s thereafter
        assert recorded == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0, 30.0]


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


class TestRequestImmediateOrderSync:
    """Fast-track path for WS-reported untracked (manual) orders."""

    def test_zeros_next_sync(self, gridbot_config):
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._next_order_sync = 999.0

        orchestrator._request_immediate_order_sync("btcusdt_test")

        assert orchestrator._next_order_sync == 0.0

    def test_debounces_repeat_calls(self, gridbot_config):
        from gridbot.orchestrator import _UNKNOWN_ORDER_DEBOUNCE_SEC

        orchestrator = Orchestrator(gridbot_config)

        fake_now = [1000.0]

        def fake_monotonic():
            return fake_now[0]

        with patch("gridbot.orchestrator.time.monotonic", side_effect=fake_monotonic):
            orchestrator._request_immediate_order_sync("btcusdt_test")
            assert orchestrator._next_order_sync == 0.0

            # Second call inside the debounce window must be a no-op.
            orchestrator._next_order_sync = 999.0
            fake_now[0] += _UNKNOWN_ORDER_DEBOUNCE_SEC / 2
            orchestrator._request_immediate_order_sync("btcusdt_test")
            assert orchestrator._next_order_sync == 999.0

            # After the window expires, it fires again.
            fake_now[0] += _UNKNOWN_ORDER_DEBOUNCE_SEC
            orchestrator._request_immediate_order_sync("btcusdt_test")
            assert orchestrator._next_order_sync == 0.0
