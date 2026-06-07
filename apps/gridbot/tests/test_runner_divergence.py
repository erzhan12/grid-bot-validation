"""Feature 0069 — runner-side pieces of the state-divergence detector (#151).

Covers signal 1's rolling placement-failure UNION recorder, the post-reconcile
``clear_dedup_cache``, and the read-only ``rest_position_size`` REST helper.

    uv run pytest apps/gridbot/tests/test_runner_divergence.py
"""

from decimal import Decimal
from itertools import cycle
from unittest.mock import Mock, MagicMock

import pytest

from gridcore import InstrumentInfo
from gridcore.intents import PlaceLimitIntent
from gridcore.position import DirectionType

from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor, OrderResult
from gridbot.runner import StrategyRunner

EMPTY_LIMITS: dict[str, list[dict]] = {"long": [], "short": []}

TRUNCATE_ERROR = "Bybit API error in place_order: [110017] orderQty will be truncated to zero"
DUP_LINK_ERROR = "Bybit API error in place_order: [110072] OrderLinkedID is duplicate"
NETWORK_ERROR = "requests.exceptions.ConnectionError: Connection timeout"
LOW_BAL_ERROR = "Bybit API error: [110007] available balance not enough for new order"


def _positions(size, position_idx=1):
    return [
        {
            "symbol": "SOLUSDT",
            "positionIdx": position_idx,
            "side": "Buy" if position_idx == 1 else "Sell",
            "size": str(size),
            "avgPrice": "84.0",
        }
    ]


def _reduce_only_sell(price="84.51", qty="0.1"):
    return PlaceLimitIntent.create(
        symbol="SOLUSDT",
        side="Sell",
        price=Decimal(price),
        qty=Decimal(qty),
        grid_level=3,
        direction="long",
        reduce_only=True,
    )


@pytest.fixture
def clock():
    return {"now": 0.0}


@pytest.fixture
def instrument_info():
    return InstrumentInfo(
        symbol="SOLUSDT",
        qty_step=Decimal("0.01"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        max_qty=Decimal("10000"),
    )


@pytest.fixture
def mock_executor():
    executor = Mock(spec=IntentExecutor)
    executor.shadow_mode = False
    executor.auth_cooldown = False
    executor.execute_place = MagicMock(
        return_value=OrderResult(success=True, order_id="order_1")
    )
    return executor


@pytest.fixture
def mock_rest_client():
    client = Mock()
    client.get_positions = MagicMock(return_value=_positions("0.05"))
    return client


@pytest.fixture
def runner_factory(mock_executor, instrument_info, mock_rest_client, clock):
    """Build a runner with overridable divergence config + signal-1 callback."""
    def _make(
        *,
        on_divergence_failure_mix=None,
        threshold=10,
        window=60.0,
        enabled=True,
    ):
        config = StrategyConfig(
            strat_id="solusdt_test",
            account="test_account",
            symbol="SOLUSDT",
            tick_size=Decimal("0.01"),
            grid_count=30,
            grid_step=0.3,
            divergence_detector_enabled=enabled,
            divergence_failure_mix_threshold=threshold,
            divergence_failure_mix_window_seconds=window,
        )
        r = StrategyRunner(
            strategy_config=config,
            executor=mock_executor,
            instrument_info=instrument_info,
            rest_client=mock_rest_client,
            clock=lambda: clock["now"],
            on_divergence_failure_mix=on_divergence_failure_mix,
        )
        r._wallet_balance = Decimal("10000")
        return r
    return _make


# --------------------------------------------------------------------------
# Signal 1 — rolling placement-failure UNION recorder
# --------------------------------------------------------------------------

def test_signal1_fires_once_at_threshold_then_window_resets(runner_factory):
    calls = []
    r = runner_factory(
        on_divergence_failure_mix=lambda *a: calls.append(a),
        threshold=10, window=60.0,
    )
    errors = cycle([TRUNCATE_ERROR, DUP_LINK_ERROR, NETWORK_ERROR])
    for _ in range(9):
        r._record_placement_failure(next(errors))
    assert calls == []  # below threshold
    r._record_placement_failure(next(errors))  # 10th → fire
    assert calls == [("solusdt_test", "rest_failure_mix", 10)]
    # The window clears at the moment of fire, so the very next failure starts
    # fresh and does NOT immediately re-fire.
    assert len(r._placement_failure_window) == 0
    r._record_placement_failure(next(errors))
    assert len(calls) == 1
    assert len(r._placement_failure_window) == 1


def test_signal1_eviction_past_window_prevents_fire(runner_factory, clock):
    """Advancing the clock past the window before the 10th failure evicts the
    earlier entries, so the threshold is NOT reached — proves eviction reads the
    injectable clock."""
    calls = []
    r = runner_factory(
        on_divergence_failure_mix=lambda *a: calls.append(a),
        threshold=10, window=60.0,
    )
    clock["now"] = 0.0
    for _ in range(9):
        r._record_placement_failure(TRUNCATE_ERROR)
    clock["now"] = 61.0  # all nine are now older than the 60s window
    r._record_placement_failure(TRUNCATE_ERROR)  # 10th, but 9 evicted → len == 1
    assert calls == []
    assert len(r._placement_failure_window) == 1


def test_signal1_excludes_110007(runner_factory):
    """110007 (low-balance, feature 0066) is an intentional drop, NOT divergence
    — it must not even enter the window."""
    calls = []
    r = runner_factory(
        on_divergence_failure_mix=lambda *a: calls.append(a), threshold=1,
    )
    r._record_placement_failure(LOW_BAL_ERROR)
    assert calls == []
    assert len(r._placement_failure_window) == 0


def test_signal1_ignores_unrelated_errors(runner_factory):
    calls = []
    r = runner_factory(
        on_divergence_failure_mix=lambda *a: calls.append(a), threshold=1,
    )
    r._record_placement_failure("some weird non-classified error")
    assert calls == []
    assert len(r._placement_failure_window) == 0


def test_signal1_inert_when_detector_disabled(runner_factory):
    calls = []
    r = runner_factory(
        on_divergence_failure_mix=lambda *a: calls.append(a),
        threshold=1, enabled=False,
    )
    r._record_placement_failure(TRUNCATE_ERROR)
    assert calls == []
    assert len(r._placement_failure_window) == 0


def test_signal1_records_via_non_110017_branch(runner_factory, mock_executor):
    """Integration: a 110072 failure leaves _execute_place_intent via the
    non-110017 early-return branch and MUST feed the recorder (threshold=1)."""
    calls = []
    r = runner_factory(
        on_divergence_failure_mix=lambda *a: calls.append(a), threshold=1,
    )
    r._long_position.size = Decimal("0.2")  # stale-high so the guard passes
    mock_executor.execute_place.return_value = OrderResult(
        success=False, error=DUP_LINK_ERROR
    )
    r._execute_place_intent(_reduce_only_sell(qty="0.1"), EMPTY_LIMITS)
    assert calls == [("solusdt_test", "rest_failure_mix", 1)]


def test_signal1_records_via_110017_branch(runner_factory, mock_executor):
    """Integration: a 110017 failure leaves via the truncate branch and MUST
    feed the recorder too (threshold=1)."""
    calls = []
    r = runner_factory(
        on_divergence_failure_mix=lambda *a: calls.append(a), threshold=1,
    )
    r._long_position.size = Decimal("0.2")
    mock_executor.execute_place.return_value = OrderResult(
        success=False, error=TRUNCATE_ERROR
    )
    r._execute_place_intent(_reduce_only_sell(qty="0.1"), EMPTY_LIMITS)
    assert calls == [("solusdt_test", "rest_failure_mix", 1)]


# --------------------------------------------------------------------------
# clear_dedup_cache
# --------------------------------------------------------------------------

def test_clear_dedup_cache_empties_cache(runner_factory):
    r = runner_factory()
    r._same_order_dedup_cache[frozenset({"a", "b"})] = object()
    r._same_order_dedup_cache[frozenset({"c", "d"})] = object()
    assert len(r._same_order_dedup_cache) == 2
    r.clear_dedup_cache()
    assert r._same_order_dedup_cache == {}


# --------------------------------------------------------------------------
# rest_position_size — PURE read, no side effects
# --------------------------------------------------------------------------

def test_rest_position_size_returns_parsed_size(runner_factory, mock_rest_client):
    r = runner_factory()
    mock_rest_client.get_positions.return_value = _positions("0.37", position_idx=1)
    assert r.rest_position_size(DirectionType.LONG) == Decimal("0.37")


def test_rest_position_size_no_entry_returns_zero(runner_factory, mock_rest_client):
    r = runner_factory()
    mock_rest_client.get_positions.return_value = _positions("0.5", position_idx=1)
    # Asking for SHORT (idx 2) when only a LONG entry exists → flat (0).
    assert r.rest_position_size(DirectionType.SHORT) == Decimal("0")


def test_rest_position_size_none_when_no_rest_client(
    mock_executor, instrument_info, clock
):
    config = StrategyConfig(
        strat_id="solusdt_test", account="test_account", symbol="SOLUSDT",
        tick_size=Decimal("0.01"), grid_count=30, grid_step=0.3,
    )
    r = StrategyRunner(
        strategy_config=config, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None,
        clock=lambda: clock["now"],
    )
    assert r.rest_position_size(DirectionType.LONG) is None


def test_rest_position_size_none_on_exception(runner_factory, mock_rest_client):
    r = runner_factory()
    mock_rest_client.get_positions.side_effect = Exception("boom")
    assert r.rest_position_size(DirectionType.LONG) is None


def test_rest_position_size_none_on_unparseable(runner_factory, mock_rest_client):
    r = runner_factory()
    mock_rest_client.get_positions.return_value = _positions("not-a-number")
    assert r.rest_position_size(DirectionType.LONG) is None


def test_rest_position_size_does_not_mutate_state(runner_factory, mock_rest_client):
    """PURE read: no mirror mutation, no throttle write, no failure-count bump."""
    r = runner_factory()
    r._long_position.size = Decimal("0.2")
    before_throttle = dict(r._last_dirty_rest_at)
    before_fail = r._dirty_rest_refresh_failure_count
    mock_rest_client.get_positions.return_value = _positions("0.99")
    out = r.rest_position_size(DirectionType.LONG)
    assert out == Decimal("0.99")
    assert r._long_position.size == Decimal("0.2")  # mirror untouched
    assert r._last_dirty_rest_at == before_throttle  # throttle untouched
    assert r._dirty_rest_refresh_failure_count == before_fail  # counter untouched


def test_rest_position_size_exception_does_not_bump_failure_counter(
    runner_factory, mock_rest_client
):
    r = runner_factory()
    before = r._dirty_rest_refresh_failure_count
    mock_rest_client.get_positions.side_effect = Exception("boom")
    assert r.rest_position_size(DirectionType.LONG) is None
    # Unlike _refresh_position_size_from_rest, the pure read must NOT increment
    # the dirty-refresh failure counter (signal 2 reads it).
    assert r._dirty_rest_refresh_failure_count == before
