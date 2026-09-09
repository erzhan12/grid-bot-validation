"""Executor <-> HealthMetrics integration (feature 0082 / issue #185)."""

from decimal import Decimal
from unittest.mock import MagicMock, Mock

from bybit_adapter.rest_client import CancelOrderResult
from gridcore.intents import CancelIntent, PlaceLimitIntent
from gridbot.executor import IntentExecutor
from gridbot.health import HealthMetrics


def _intent():
    return PlaceLimitIntent.create(
        symbol="BTCUSDT", side="Buy", price=Decimal("50000.0"),
        qty=Decimal("0.001"), grid_level=10, direction="long",
    )


def _client(place_ok=True, error=None):
    c = Mock()
    if place_ok:
        c.place_order = MagicMock(return_value={"orderId": "oid1"})
    else:
        c.place_order = MagicMock(side_effect=Exception(error))
    c.cancel_order = MagicMock(
        return_value=CancelOrderResult(
            success=True,
            benign=False,
            ret_code=0,
            ret_msg="OK",
            exception=None,
        )
    )
    return c


def test_live_place_success_bumps_orders_placed():
    m = HealthMetrics()
    ex = IntentExecutor(_client(), shadow_mode=False, health_metrics=m)
    ex.execute_place(_intent())
    assert m.orders_placed == 1
    assert m.orders_placed_shadow == 0


def test_shadow_place_bumps_shadow_only():
    m = HealthMetrics()
    ex = IntentExecutor(_client(), shadow_mode=True, health_metrics=m)
    ex.execute_place(_intent())
    assert m.orders_placed_shadow == 1
    assert m.orders_placed == 0
    assert not m.rest_errors_by_code  # shadow never touches the wire


def test_insufficient_balance_failure_bumps_reject_and_rest_code():
    m = HealthMetrics()
    ex = IntentExecutor(
        _client(place_ok=False, error="Bybit API error in place_order: [110007] ab not enough"),
        shadow_mode=False, health_metrics=m,
    )
    res = ex.execute_place(_intent())
    assert res.success is False
    assert m.orders_rejected["insufficient_balance"] == 1
    assert m.rest_errors_by_code["110007"] == 1
    assert m.orders_placed == 0


def test_cancel_success_bumps_cancels():
    m = HealthMetrics()
    ex = IntentExecutor(_client(), shadow_mode=False, health_metrics=m)
    ex.execute_cancel(CancelIntent(symbol="BTCUSDT", order_id="x", reason="rebuild"))
    assert m.cancels == 1 and m.cancels_failed == 0


def test_benign_cancel_failure_bumps_cancel_failed_only():
    """Benign terminal-order races count only as failed cancels."""
    m = HealthMetrics()
    client = _client()
    client.cancel_order.return_value = CancelOrderResult(
        success=False,
        benign=True,
        ret_code=110001,
        ret_msg="Order does not exist",
        exception=None,
    )
    ex = IntentExecutor(client, shadow_mode=False, health_metrics=m)

    result = ex.execute_cancel(CancelIntent(symbol="BTCUSDT", order_id="x", reason="rebuild"))

    assert result.success is False
    assert m.cancels_failed == 1
    assert not m.rest_errors_by_code
    assert not m.orders_rejected


def test_unexpected_cancel_failure_bumps_other_rest_error():
    """Unexpected cancellation retCodes record the existing other REST bucket."""
    m = HealthMetrics()
    client = _client()
    client.cancel_order.return_value = CancelOrderResult(
        success=False,
        benign=False,
        ret_code=10001,
        ret_msg="Parameter error",
        exception=None,
    )
    ex = IntentExecutor(client, shadow_mode=False, health_metrics=m)

    ex.execute_cancel(CancelIntent(symbol="BTCUSDT", order_id="x", reason="rebuild"))

    assert m.cancels_failed == 1
    assert m.rest_errors_by_code["other"] == 1
    assert not m.orders_rejected


def test_network_cancel_failure_bumps_network_rest_error():
    """Transport cancellation failures record the network REST bucket."""
    m = HealthMetrics()
    client = _client()
    client.cancel_order.return_value = CancelOrderResult(
        success=False,
        benign=False,
        ret_code=None,
        ret_msg=None,
        exception=ConnectionError("Connection timeout"),
    )
    ex = IntentExecutor(client, shadow_mode=False, health_metrics=m)

    ex.execute_cancel(CancelIntent(symbol="BTCUSDT", order_id="x", reason="rebuild"))

    assert m.cancels_failed == 1
    assert m.rest_errors_by_code["network"] == 1
    assert not m.orders_rejected


def test_auth_cancel_failure_bumps_auth_rest_error_without_order_reject():
    """Authentication cancel failures do not become placement rejects."""
    m = HealthMetrics()
    client = _client()
    client.cancel_order.return_value = CancelOrderResult(
        success=False,
        benign=False,
        ret_code=10005,
        ret_msg="Permission denied",
        exception=None,
    )
    ex = IntentExecutor(client, shadow_mode=False, health_metrics=m)

    ex.execute_cancel(CancelIntent(symbol="BTCUSDT", order_id="x", reason="rebuild"))

    assert m.cancels_failed == 1
    assert m.rest_errors_by_code["auth"] == 1
    assert not m.orders_rejected


def test_metrics_optional_none_is_inert():
    ex = IntentExecutor(_client(), shadow_mode=False)  # no health_metrics
    assert ex.execute_place(_intent()).success is True
