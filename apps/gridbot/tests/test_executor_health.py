"""Executor <-> HealthMetrics integration (feature 0082 / issue #185)."""

from decimal import Decimal
from unittest.mock import MagicMock, Mock

from gridcore.intents import PlaceLimitIntent
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
    c.cancel_order = MagicMock(return_value=True)
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
    from gridcore.intents import CancelIntent
    ex.execute_cancel(CancelIntent(symbol="BTCUSDT", order_id="x", reason="rebuild"))
    assert m.cancels == 1 and m.cancels_failed == 0


def test_metrics_optional_none_is_inert():
    ex = IntentExecutor(_client(), shadow_mode=False)  # no health_metrics
    assert ex.execute_place(_intent()).success is True
