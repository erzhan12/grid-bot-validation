"""Tests for gridbot orchestrator module."""

import asyncio
from decimal import Decimal
from unittest.mock import Mock, MagicMock, patch, AsyncMock

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

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_init_account(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
        account_config,
    ):
        """Test account initialization."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)

        assert "test_account" in orchestrator._rest_clients
        assert "test_account" in orchestrator._executors
        assert "test_account" in orchestrator._reconcilers
        assert "test_account" in orchestrator._public_ws
        assert "test_account" in orchestrator._private_ws

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_init_strategy(
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
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)

        assert "btcusdt_test" in orchestrator._runners
        assert "btcusdt_test" in orchestrator._retry_queues


class TestOrchestratorRouting:
    """Tests for event routing."""

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_build_routing_maps(
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
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()

        # Check symbol routing
        assert "BTCUSDT" in orchestrator._symbol_to_runners
        assert len(orchestrator._symbol_to_runners["BTCUSDT"]) == 1

        # Check account routing
        assert "test_account" in orchestrator._account_to_runners
        assert len(orchestrator._account_to_runners["test_account"]) == 1

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_get_account_for_strategy(
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

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_multiple_strategies_same_account(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        multi_config,
        account_config,
    ):
        """Test multiple strategies on same account."""
        orchestrator = Orchestrator(multi_config)
        await orchestrator._init_account(account_config)

        for strategy in multi_config.strategies:
            await orchestrator._init_strategy(strategy)

        orchestrator._build_routing_maps()

        # Should have 2 runners
        assert len(orchestrator._runners) == 2

        # Account should have 2 runners
        assert len(orchestrator._account_to_runners["test_account"]) == 2

        # Each symbol should have 1 runner
        assert len(orchestrator._symbol_to_runners["BTCUSDT"]) == 1
        assert len(orchestrator._symbol_to_runners["ETHUSDT"]) == 1


class TestOrchestratorLifecycle:
    """Tests for orchestrator lifecycle."""

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_start_sets_running(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
    ):
        """Test start sets running flag."""
        # Mock WebSocket connect methods
        mock_public_ws.return_value.connect = Mock()
        mock_public_ws.return_value.subscribe_ticker = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_private_ws.return_value.subscribe_position = Mock()
        mock_private_ws.return_value.subscribe_order = Mock()
        mock_private_ws.return_value.subscribe_execution = Mock()

        # Mock REST client methods
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)
        await orchestrator.start()

        assert orchestrator.running is True

        await orchestrator.stop()
        assert orchestrator.running is False

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_stop_disconnects_websockets(
        self,
        mock_private_ws,
        mock_public_ws,
        mock_rest_client,
        gridbot_config,
    ):
        """Test stop disconnects WebSockets."""
        mock_public_ws.return_value.connect = Mock()
        mock_public_ws.return_value.subscribe_ticker = Mock()
        mock_public_ws.return_value.disconnect = Mock()
        mock_private_ws.return_value.connect = Mock()
        mock_private_ws.return_value.subscribe_position = Mock()
        mock_private_ws.return_value.subscribe_order = Mock()
        mock_private_ws.return_value.subscribe_execution = Mock()
        mock_private_ws.return_value.disconnect = Mock()
        mock_rest_client.return_value.get_open_orders = Mock(return_value=[])

        orchestrator = Orchestrator(gridbot_config)
        await orchestrator.start()
        await orchestrator.stop()

        # Verify disconnect was called
        mock_public_ws.return_value.disconnect.assert_called()
        mock_private_ws.return_value.disconnect.assert_called()


class TestOrchestratorGuardClauses:
    """Tests for guard clauses in start/stop and run_until_shutdown."""

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_start_already_running_returns_early(
        self, mock_private_ws, mock_public_ws, mock_rest_client, gridbot_config
    ):
        """Test start() returns immediately when already running."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._running = True
        await orchestrator.start()
        assert len(orchestrator._runners) == 0

    @pytest.mark.asyncio
    async def test_stop_not_running_returns_early(self, gridbot_config):
        """Test stop() returns immediately when not running."""
        orchestrator = Orchestrator(gridbot_config)
        assert not orchestrator._running
        await orchestrator.stop()
        assert not orchestrator._running

    @pytest.mark.asyncio
    async def test_run_until_shutdown(self, gridbot_config):
        """Test run_until_shutdown returns when shutdown event is set."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._shutdown_event.set()
        await orchestrator.run_until_shutdown()


class TestOrchestratorPositionWsCache:
    """Tests for WebSocket position data caching."""

    def test_on_position_stores_linear_data(self, gridbot_config):
        """Test _on_position stores linear position data in cache."""
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
        """Test _on_position stores both Buy and Sell positions."""
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
        """Test _on_position ignores non-linear positions."""
        orchestrator = Orchestrator(gridbot_config)
        message = {
            "data": [
                {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "size": "1.0"},
            ]
        }
        orchestrator._on_position("test_account", message)
        # Account key is created but no symbol data stored
        assert len(orchestrator._position_ws_data.get("test_account", {})) == 0

    def test_on_position_skips_empty_symbol_or_side(self, gridbot_config):
        """Test _on_position skips entries with empty symbol or side."""
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
        """Test _on_position handles empty or missing data gracefully."""
        orchestrator = Orchestrator(gridbot_config)
        orchestrator._on_position("test_account", {"data": []})
        orchestrator._on_position("test_account", {})

    def test_get_position_from_ws_returns_cached_data(self, gridbot_config):
        """Test _get_position_from_ws returns data when available."""
        orchestrator = Orchestrator(gridbot_config)
        pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"}
        orchestrator._position_ws_data = {"acct": {"BTCUSDT": {"Buy": pos}}}

        result = orchestrator._get_position_from_ws("acct", "BTCUSDT", "Buy")
        assert result == pos

    def test_get_position_from_ws_returns_none_when_missing(self, gridbot_config):
        """Test _get_position_from_ws returns None for missing data."""
        orchestrator = Orchestrator(gridbot_config)
        assert orchestrator._get_position_from_ws("missing", "BTCUSDT", "Buy") is None

        orchestrator._position_ws_data = {"acct": {}}
        assert orchestrator._get_position_from_ws("acct", "BTCUSDT", "Buy") is None

        orchestrator._position_ws_data = {"acct": {"BTCUSDT": {}}}
        assert orchestrator._get_position_from_ws("acct", "BTCUSDT", "Buy") is None


class TestOrchestratorEventHandlers:
    """Tests for WebSocket event handler routing."""

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_on_ticker_routes_to_runner(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test _on_ticker normalizes and routes event to runner."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._event_loop = asyncio.get_running_loop()

        mock_event = Mock()
        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_ticker.return_value = mock_event

        runner = orchestrator._runners["btcusdt_test"]
        runner.on_ticker = AsyncMock(return_value=[])

        orchestrator._on_ticker("test_account", "BTCUSDT", {"topic": "tickers.BTCUSDT"})
        await asyncio.sleep(0.05)

        runner.on_ticker.assert_called_once_with(mock_event)

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_on_ticker_none_event_skipped(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test _on_ticker skips when normalizer returns None."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._event_loop = asyncio.get_running_loop()

        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_ticker.return_value = None

        runner = orchestrator._runners["btcusdt_test"]
        runner.on_ticker = AsyncMock(return_value=[])

        orchestrator._on_ticker("test_account", "BTCUSDT", {})
        await asyncio.sleep(0.05)

        runner.on_ticker.assert_not_called()

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_on_order_routes_to_runner(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test _on_order normalizes and routes events to runner."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._event_loop = asyncio.get_running_loop()

        mock_event = Mock()
        mock_event.symbol = "BTCUSDT"
        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_order.return_value = [mock_event]

        runner = orchestrator._runners["btcusdt_test"]
        runner.on_order_update = AsyncMock(return_value=[])

        orchestrator._on_order("test_account", {"topic": "order"})
        await asyncio.sleep(0.05)

        runner.on_order_update.assert_called_once_with(mock_event)

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_on_execution_routes_to_runner(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test _on_execution normalizes and routes events to runner."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._event_loop = asyncio.get_running_loop()

        mock_event = Mock()
        mock_event.symbol = "BTCUSDT"
        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_execution.return_value = [mock_event]

        runner = orchestrator._runners["btcusdt_test"]
        runner.on_execution = AsyncMock(return_value=[])

        orchestrator._on_execution("test_account", {"topic": "execution"})
        await asyncio.sleep(0.05)

        runner.on_execution.assert_called_once_with(mock_event)


class TestOrchestratorPositionCheckLoop:
    """Tests for the periodic position check loop."""

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_position_check_uses_ws_data(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test position check uses WebSocket data when available."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Replace runner with mock
        mock_runner = Mock()
        mock_runner.strat_id = "btcusdt_test"
        mock_runner.symbol = "BTCUSDT"
        mock_runner.engine.last_close = 42000.0
        mock_runner.on_position_update = AsyncMock()
        orchestrator._account_to_runners["test_account"] = [mock_runner]

        # Pre-populate WS position cache
        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.05"}
        orchestrator._position_ws_data = {
            "test_account": {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}}
        }

        # Mock REST client wallet balance
        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]
        }

        # Run one iteration then stop
        async def stop_after_first(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_after_first):
            await orchestrator._position_check_loop()

        mock_runner.on_position_update.assert_called_once_with(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=42000.0,
        )
        rest_client.get_positions.assert_not_called()

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_position_check_falls_back_to_rest(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test position check falls back to REST when WS data is missing."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Replace runner with mock
        mock_runner = Mock()
        mock_runner.strat_id = "btcusdt_test"
        mock_runner.symbol = "BTCUSDT"
        mock_runner.engine.last_close = 42000.0
        mock_runner.on_position_update = AsyncMock()
        orchestrator._account_to_runners["test_account"] = [mock_runner]

        # NO WS data
        orchestrator._position_ws_data = {}

        # Mock REST client
        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "5000"}]}]
        }
        long_pos_rest = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.2"}
        short_pos_rest = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.1"}
        other_pos = {"symbol": "ETHUSDT", "side": "Buy", "size": "1.0"}  # unrelated symbol
        rest_client.get_positions.return_value = [other_pos, long_pos_rest, short_pos_rest]

        async def stop_after_first(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_after_first):
            await orchestrator._position_check_loop()

        rest_client.get_positions.assert_called_once()
        mock_runner.on_position_update.assert_called_once_with(
            long_position=long_pos_rest,
            short_position=short_pos_rest,
            wallet_balance=5000.0,
            last_close=42000.0,
        )

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_position_check_handles_account_error(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test position check catches and logs per-account errors."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Make REST client raise error
        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.side_effect = Exception("API error")

        async def stop_after_first(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_after_first):
            await orchestrator._position_check_loop()
        # Should not raise — error is caught and logged


class TestOrchestratorDbRecords:
    """Tests for database record stubs."""

    @pytest.mark.asyncio
    async def test_create_run_records_with_db(self, gridbot_config):
        """Test _create_run_records executes when db is present."""
        orchestrator = Orchestrator(gridbot_config, db=Mock())
        await orchestrator._create_run_records()

    @pytest.mark.asyncio
    async def test_update_run_records_stopped_with_db(self, gridbot_config):
        """Test _update_run_records_stopped executes when db is present."""
        orchestrator = Orchestrator(gridbot_config, db=Mock())
        await orchestrator._update_run_records_stopped()


class TestOrchestratorRetryDispatcher:
    """Tests for retry queue intent dispatch routing."""

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_retry_dispatcher_routes_cancel_to_execute_cancel(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test retry queue dispatches CancelIntent to execute_cancel, not execute_place."""
        from gridcore.intents import CancelIntent, PlaceLimitIntent
        from unittest.mock import call

        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)

        # Get the executor that was created for this strategy
        executor = orchestrator._runners["btcusdt_test"]._executor

        # Get the retry queue's executor function (the dispatcher)
        retry_queue = orchestrator._retry_queues["btcusdt_test"]
        dispatcher = retry_queue._executor_func

        cancel = CancelIntent(symbol="BTCUSDT", order_id="order_123", reason="test")

        # Reset mocks to track only our call
        executor.execute_cancel = MagicMock(return_value=Mock(success=True))
        executor.execute_place = MagicMock(return_value=Mock(success=True))

        dispatcher(cancel)

        executor.execute_cancel.assert_called_once_with(cancel)
        executor.execute_place.assert_not_called()

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_retry_dispatcher_routes_place_to_execute_place(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test retry queue dispatches PlaceLimitIntent to execute_place."""
        from gridcore.intents import PlaceLimitIntent

        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)

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

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_on_ticker_exception_does_not_crash(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test _on_ticker catches exceptions and notifies."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._event_loop = asyncio.get_running_loop()

        # Make normalizer raise
        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_ticker.side_effect = ValueError("bad data")

        # Should not raise
        orchestrator._on_ticker("test_account", "BTCUSDT", {})

        notifier.alert_exception.assert_called_once()
        assert "on_ticker" in notifier.alert_exception.call_args[0][0]

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_on_order_exception_does_not_crash(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test _on_order catches exceptions and notifies."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._event_loop = asyncio.get_running_loop()

        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_order.side_effect = KeyError("missing")

        orchestrator._on_order("test_account", {})

        notifier.alert_exception.assert_called_once()
        assert "on_order" in notifier.alert_exception.call_args[0][0]

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_on_execution_exception_does_not_crash(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test _on_execution catches exceptions and notifies."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._event_loop = asyncio.get_running_loop()

        orchestrator._normalizers["test_account"] = Mock()
        orchestrator._normalizers["test_account"].normalize_execution.side_effect = TypeError("oops")

        orchestrator._on_execution("test_account", {})

        notifier.alert_exception.assert_called_once()
        assert "on_execution" in notifier.alert_exception.call_args[0][0]

    def test_on_position_exception_does_not_crash(self, gridbot_config):
        """Test _on_position catches broad exceptions and notifies."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)

        # Pass data that will raise inside the handler
        # message["data"] is not iterable
        orchestrator._on_position("test_account", {"data": 12345})

        notifier.alert_exception.assert_called_once()
        assert "on_position" in notifier.alert_exception.call_args[0][0]


class TestOrchestratorHealthCheckLoop:
    """Tests for WebSocket health check loop."""

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_health_check_reconnects_disconnected_public_ws(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test health check reconnects a disconnected public WebSocket."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Simulate public WS disconnected
        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = False
        pub_ws.connect = Mock()
        pub_ws.disconnect = Mock()
        pub_ws.subscribe_ticker = Mock()

        # Private WS is fine
        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = True

        async def stop_immediately(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_immediately):
            await orchestrator._health_check_loop()

        pub_ws.disconnect.assert_called_once()
        pub_ws.connect.assert_called_once()
        pub_ws.subscribe_ticker.assert_called()
        notifier.alert.assert_called()

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_health_check_reconnects_disconnected_private_ws(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test health check reconnects a disconnected private WebSocket."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Public WS is fine
        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = True

        # Simulate private WS disconnected
        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = False
        priv_ws.connect = Mock()
        priv_ws.disconnect = Mock()
        priv_ws.subscribe_position = Mock()
        priv_ws.subscribe_order = Mock()
        priv_ws.subscribe_execution = Mock()

        async def stop_immediately(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_immediately):
            await orchestrator._health_check_loop()

        priv_ws.disconnect.assert_called_once()
        priv_ws.connect.assert_called_once()
        priv_ws.subscribe_position.assert_called()
        priv_ws.subscribe_order.assert_called()
        priv_ws.subscribe_execution.assert_called()
        notifier.alert.assert_called()

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_health_check_skips_connected(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test health check does nothing when all WS are connected."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = True

        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = True

        async def stop_immediately(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_immediately):
            await orchestrator._health_check_loop()

        # No reconnect calls, no alerts
        notifier.alert.assert_not_called()
        notifier.alert_exception.assert_not_called()

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_health_check_reconnect_failure_notifies(
        self, mock_private_ws_cls, mock_public_ws_cls, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test health check notifies on reconnect failure."""
        notifier = Mock(spec=Notifier)
        orchestrator = Orchestrator(gridbot_config, notifier=notifier)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Public WS disconnected and reconnect fails
        pub_ws = orchestrator._public_ws["test_account"]
        pub_ws.is_connected.return_value = False
        pub_ws.disconnect = Mock()
        pub_ws.connect = Mock(side_effect=Exception("connection refused"))

        # Private WS fine
        priv_ws = orchestrator._private_ws["test_account"]
        priv_ws.is_connected.return_value = True

        async def stop_immediately(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_immediately):
            await orchestrator._health_check_loop()

        # Should have alert for disconnect + alert_exception for reconnect failure
        assert notifier.alert.call_count >= 1
        assert notifier.alert_exception.call_count >= 1


class TestOrchestratorOrderSyncLoop:
    """Tests for the periodic order reconciliation loop."""

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_order_sync_loop_calls_reconcile_reconnect(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test order sync loop calls reconcile_reconnect periodically."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Mock reconciler
        reconciler = orchestrator._reconcilers["test_account"]
        reconciler.reconcile_reconnect = AsyncMock()

        # Mock ReconciliationResult

        reconciler.reconcile_reconnect.return_value = ReconciliationResult(
            orders_fetched=5,
            orders_injected=0,
            orphan_orders=0,
        )

        # Run one iteration then stop
        async def stop_after_first(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_after_first):
            await orchestrator._order_sync_loop()

        # Should have called reconcile_reconnect for our runner
        reconciler.reconcile_reconnect.assert_called_once()
        runner = orchestrator._runners["btcusdt_test"]
        reconciler.reconcile_reconnect.assert_called_with(runner)

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_order_sync_loop_disabled_when_interval_zero(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        account_config, strategy_config,
    ):
        """Test order sync loop is disabled when order_sync_interval is 0."""
        # Create config with order_sync_interval = 0
        config = GridbotConfig(
            accounts=[account_config],
            strategies=[strategy_config],
            order_sync_interval=0.0,
        )
        orchestrator = Orchestrator(config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Mock reconciler - should not be called
        reconciler = orchestrator._reconcilers["test_account"]
        reconciler.reconcile_reconnect = AsyncMock()

        # Run the loop
        await orchestrator._order_sync_loop()

        # Should return early and not call reconcile
        reconciler.reconcile_reconnect.assert_not_called()

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_order_sync_loop_handles_errors(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test order sync loop catches and logs errors."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Make reconciler raise error
        reconciler = orchestrator._reconcilers["test_account"]
        reconciler.reconcile_reconnect = AsyncMock(side_effect=Exception("API error"))

        async def stop_after_first(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_after_first):
            await orchestrator._order_sync_loop()

        # Should not raise — error is caught and logged

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_order_sync_loop_logs_discrepancies(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test order sync loop logs when discrepancies are found."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)
        await orchestrator._init_strategy(strategy_config)
        orchestrator._build_routing_maps()
        orchestrator._running = True

        # Mock reconciler to return discrepancies

        reconciler = orchestrator._reconcilers["test_account"]
        reconciler.reconcile_reconnect = AsyncMock()
        reconciler.reconcile_reconnect.return_value = ReconciliationResult(
            orders_fetched=10,
            orders_injected=2,
            orphan_orders=1,
        )

        async def stop_after_first(seconds):
            orchestrator._running = False

        with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=stop_after_first):
            await orchestrator._order_sync_loop()

        # Should have called reconcile
        reconciler.reconcile_reconnect.assert_called_once()


class TestOrchestratorWalletCache:
    """Tests for wallet balance caching."""

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_get_wallet_balance_caches_result(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test wallet balance is cached on first fetch."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)

        # Mock REST client
        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "5000"}]}]
        }

        # First call should fetch and cache
        balance = await orchestrator._get_wallet_balance("test_account")
        assert balance == 5000.0
        rest_client.get_wallet_balance.assert_called_once()

        # Cache should be populated
        assert "test_account" in orchestrator._wallet_cache
        cached_balance, timestamp = orchestrator._wallet_cache["test_account"]
        assert cached_balance == 5000.0

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_get_wallet_balance_returns_cached_value(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test subsequent calls return cached value within interval."""
        from datetime import datetime, UTC

        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)

        # Pre-populate cache with recent timestamp
        orchestrator._wallet_cache["test_account"] = (10000.0, datetime.now(UTC))

        # Mock REST client
        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "9999"}]}]
        }

        # Should return cached value without calling REST
        balance = await orchestrator._get_wallet_balance("test_account")
        assert balance == 10000.0
        rest_client.get_wallet_balance.assert_not_called()

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_get_wallet_balance_refreshes_after_expiry(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Test cache expires and refetches after interval."""
        from datetime import datetime, UTC, timedelta

        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)

        # Pre-populate cache with old timestamp (expired)
        old_timestamp = datetime.now(UTC) - timedelta(seconds=400)
        orchestrator._wallet_cache["test_account"] = (5000.0, old_timestamp)

        # Mock REST client with new balance
        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "7500"}]}]
        }

        # Should fetch fresh value and update cache
        balance = await orchestrator._get_wallet_balance("test_account")
        assert balance == 7500.0
        rest_client.get_wallet_balance.assert_called_once()

        # Cache should be updated with new value
        cached_balance, timestamp = orchestrator._wallet_cache["test_account"]
        assert cached_balance == 7500.0

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_get_wallet_balance_disabled_when_interval_zero(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        account_config, strategy_config,
    ):
        """Test caching is disabled when wallet_cache_interval is 0."""
        from datetime import datetime, UTC

        # Create config with wallet_cache_interval = 0
        config = GridbotConfig(
            accounts=[account_config],
            strategies=[strategy_config],
            wallet_cache_interval=0.0,
        )
        orchestrator = Orchestrator(config)
        await orchestrator._init_account(account_config)

        # Pre-populate cache (should be ignored)
        orchestrator._wallet_cache["test_account"] = (5000.0, datetime.now(UTC))

        # Mock REST client
        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "8000"}]}]
        }

        # Should always fetch fresh, ignore cache
        balance = await orchestrator._get_wallet_balance("test_account")
        assert balance == 8000.0
        rest_client.get_wallet_balance.assert_called_once()

        # Call again - should fetch again (no caching)
        rest_client.get_wallet_balance.reset_mock()
        balance = await orchestrator._get_wallet_balance("test_account")
        assert balance == 8000.0
        rest_client.get_wallet_balance.assert_called_once()

    @pytest.mark.asyncio
    @patch("gridbot.orchestrator.BybitRestClient")
    @patch("gridbot.orchestrator.PublicWebSocketClient")
    @patch("gridbot.orchestrator.PrivateWebSocketClient")
    async def test_get_wallet_balance_concurrent_deduplicates(
        self, mock_private_ws, mock_public_ws, mock_rest_client,
        gridbot_config, account_config, strategy_config,
    ):
        """Concurrent cache misses should issue only one REST call."""
        orchestrator = Orchestrator(gridbot_config)
        await orchestrator._init_account(account_config)

        rest_client = orchestrator._rest_clients["test_account"]
        rest_client.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "5000"}]}]
        }

        # Launch two concurrent calls — both see empty cache
        results = await asyncio.gather(
            orchestrator._get_wallet_balance("test_account"),
            orchestrator._get_wallet_balance("test_account"),
        )

        assert results == [5000.0, 5000.0]
        # Lock ensures only one fetch, second caller hits cache
        rest_client.get_wallet_balance.assert_called_once()
