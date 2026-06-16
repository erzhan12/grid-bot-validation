"""Feature 0064 — 110017 retry-storm self-heal + circuit-breaker (issue #149).

Two post-fix paths are tested separately because they have different
preconditions:
  - primary self-heal: dirty-mirror REST refresh BEFORE the guard, so the
    unchanged strict-`>` guard rejects an oversized reduce-only close locally
    (no submit, no second 110017, breaker never trips).
  - backstop: when the refresh cannot heal (disabled / no rest client / REST
    still stale), the circuit-breaker bounds the storm after N 110017s.
"""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock, MagicMock

import pytest

from gridcore import InstrumentInfo
from gridcore.intents import PlaceLimitIntent
from gridcore.position import DirectionType

from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor, OrderResult
from gridbot.runner import StrategyRunner

EMPTY_LIMITS: dict[str, list[dict]] = {"long": [], "short": []}

TRUNCATE_ERROR = (
    "Bybit API error in place_order: [110017] orderQty will be truncated to zero"
)


def _reduce_only_sell(price="84.51", qty="0.1"):
    """A reduce-only Sell closing a LONG position (direction='long')."""
    return PlaceLimitIntent.create(
        symbol="SOLUSDT",
        side="Sell",
        price=Decimal(price),
        qty=Decimal(qty),
        grid_level=3,
        direction="long",
        reduce_only=True,
    )


def _positions(size, position_idx=1):
    """Raw Bybit get_positions() flat list with one hedge-mode entry."""
    return [
        {
            "symbol": "SOLUSDT",
            "positionIdx": position_idx,
            "side": "Buy" if position_idx == 1 else "Sell",
            "size": str(size),
            "avgPrice": "84.0",
        }
    ]


@pytest.fixture
def clock():
    """Mutable virtual clock; default pinned at 0.0 (proves clock-independence)."""
    return {"now": 0.0}


@pytest.fixture
def strategy_config():
    return StrategyConfig(
        strat_id="solusdt_test",
        account="test_account",
        symbol="SOLUSDT",
        tick_size=Decimal("0.01"),
        grid_count=30,
        grid_step=0.3,
        dirty_rest_refresh_min_interval_seconds=10.0,
        truncate_breaker_max_consecutive=3,
        truncate_breaker_window_seconds=60.0,
        truncate_breaker_cooldown_seconds=60.0,
    )


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
def reconcile_calls():
    return []


@pytest.fixture
def runner(strategy_config, mock_executor, instrument_info, mock_rest_client, clock, reconcile_calls):
    r = StrategyRunner(
        strategy_config=strategy_config,
        executor=mock_executor,
        instrument_info=instrument_info,
        rest_client=mock_rest_client,
        clock=lambda: clock["now"],
        on_truncate_breaker_tripped=lambda sid, direction: reconcile_calls.append((sid, direction)),
    )
    r._wallet_balance = Decimal("10000")
    return r


# --------------------------------------------------------------------------
# Reproducing tests — the storm and its two post-fix paths
# --------------------------------------------------------------------------

def test_110017_storm_self_heals_after_one_failure(runner, mock_executor, mock_rest_client, clock):
    """Primary path (#149 fix): storm collapses to exactly one 110017.

    Tick 1: stale-high mirror passes the guard → 110017 → dirty set.
    Tick 2+: pre-guard REST refresh corrects the mirror → guard rejects
    locally → execute_place is never called again; 110017 never enqueued;
    breaker records one event and does NOT trip.
    """
    enqueued = []
    runner._on_intent_failed = lambda intent, error: enqueued.append((intent, error))

    mock_executor.execute_place.return_value = OrderResult(success=False, error=TRUNCATE_ERROR)
    mock_rest_client.get_positions.return_value = _positions("0.05")  # actual small
    runner._long_position.size = Decimal("0.2")  # stale-high

    intent = _reduce_only_sell(qty="0.1")
    for _ in range(5):  # per-tick re-emission
        runner._execute_place_intent(replace(intent), EMPTY_LIMITS)

    assert mock_executor.execute_place.call_count == 1  # storm collapsed
    assert mock_rest_client.get_positions.call_count == 1  # tick-2 refresh, then throttled
    assert enqueued == []  # 110017 never enqueued to retry queue
    assert runner._position_dirty[DirectionType.LONG] is True
    assert runner.truncate_breaker_reconcile_count == 0  # did not trip


def test_tick2_refresh_fires_at_clock_zero(runner, mock_executor, mock_rest_client, clock):
    """Clock-independence (P2): the first dirty refresh fires even at now=0.

    Proves the `_last_dirty_rest_at is None` sentinel bypasses the throttle,
    not a large real monotonic clock.
    """
    clock["now"] = 0.0
    mock_executor.execute_place.return_value = OrderResult(success=False, error=TRUNCATE_ERROR)
    mock_rest_client.get_positions.return_value = _positions("0.05")
    runner._long_position.size = Decimal("0.2")

    intent = _reduce_only_sell(qty="0.1")
    runner._execute_place_intent(replace(intent), EMPTY_LIMITS)  # tick 1: 110017 → dirty
    assert mock_rest_client.get_positions.call_count == 0
    runner._execute_place_intent(replace(intent), EMPTY_LIMITS)  # tick 2: refresh fires at now=0
    assert mock_rest_client.get_positions.call_count == 1


def test_breaker_bounds_storm_when_refresh_cannot_heal(
    strategy_config, mock_executor, instrument_info, mock_rest_client, clock, reconcile_calls
):
    """Backstop path: refresh disabled → breaker trips and bounds the storm."""
    cfg = strategy_config.model_copy(update={"dirty_refresh_enabled": False})
    r = StrategyRunner(
        strategy_config=cfg,
        executor=mock_executor,
        instrument_info=instrument_info,
        rest_client=mock_rest_client,
        clock=lambda: clock["now"],
        on_truncate_breaker_tripped=lambda sid, d: reconcile_calls.append((sid, d)),
    )
    r._wallet_balance = Decimal("10000")
    r._long_position.size = Decimal("0.2")  # stays stale (no refresh)

    enqueued = []
    r._on_intent_failed = lambda intent, error: enqueued.append((intent, error))
    mock_executor.execute_place.return_value = OrderResult(success=False, error=TRUNCATE_ERROR)

    intent = _reduce_only_sell(qty="0.1")
    for _ in range(8):
        r._execute_place_intent(replace(intent), EMPTY_LIMITS)

    # Guard keeps passing (stale mirror) → submits until the breaker trips at N=3
    assert mock_executor.execute_place.call_count == 3
    assert r.truncate_breaker_reconcile_count == 1  # one trip
    assert reconcile_calls == [("solusdt_test", DirectionType.LONG)]  # forced reconcile once
    assert enqueued == []  # 110017 never enqueued
    assert mock_rest_client.get_positions.call_count == 0  # refresh disabled


def test_breaker_bounds_storm_when_rest_client_none(
    strategy_config, mock_executor, instrument_info, clock, reconcile_calls
):
    """Backstop path #2 (P3.1): rest_client=None can't heal → breaker bounds it.

    dirty_refresh_enabled stays True (default), so the refresh IS attempted each
    dirty tick but returns early (no client) leaving the mirror stale — the guard
    keeps passing until the breaker trips.
    """
    r = StrategyRunner(
        strategy_config=strategy_config,
        executor=mock_executor,
        instrument_info=instrument_info,
        rest_client=None,
        clock=lambda: clock["now"],
        on_truncate_breaker_tripped=lambda sid, d: reconcile_calls.append((sid, d)),
    )
    r._wallet_balance = Decimal("10000")
    r._long_position.size = Decimal("0.2")  # stale-high, no REST to correct it
    enqueued = []
    r._on_intent_failed = lambda intent, error: enqueued.append((intent, error))
    mock_executor.execute_place.return_value = OrderResult(success=False, error=TRUNCATE_ERROR)

    intent = _reduce_only_sell(qty="0.1")
    for _ in range(8):
        r._execute_place_intent(replace(intent), EMPTY_LIMITS)

    assert mock_executor.execute_place.call_count == 3  # trips at N=3
    assert r.truncate_breaker_reconcile_count == 1
    assert reconcile_calls == [("solusdt_test", DirectionType.LONG)]
    assert enqueued == []  # 110017 never enqueued


# --------------------------------------------------------------------------
# Phase 2 — dirty-mirror refresh before the guard
# --------------------------------------------------------------------------

def test_first_110017_sets_position_dirty(runner, mock_executor):
    mock_executor.execute_place.return_value = OrderResult(success=False, error=TRUNCATE_ERROR)
    runner._long_position.size = Decimal("0.2")
    runner._execute_place_intent(_reduce_only_sell(qty="0.1"), EMPTY_LIMITS)
    assert runner._position_dirty[DirectionType.LONG] is True


def test_dirty_refresh_before_guard_rejects_oversized_close(runner, mock_executor, mock_rest_client):
    runner._long_position.size = Decimal("0.2")  # stale-high
    mock_executor.execute_place.return_value = OrderResult(success=False, error=TRUNCATE_ERROR)
    mock_rest_client.get_positions.return_value = _positions("0.05")  # actual

    intent = _reduce_only_sell(qty="0.1")
    runner._execute_place_intent(replace(intent), EMPTY_LIMITS)  # 110017 → dirty
    runner._execute_place_intent(replace(intent), EMPTY_LIMITS)  # refresh → guard rejects

    assert runner._long_position.size == Decimal("0.05")
    assert mock_executor.execute_place.call_count == 1


def test_dirty_refresh_unblocks_legit_close_when_actual_sufficient(runner, mock_executor, mock_rest_client):
    """Stale-low mirror would wrongly reject; refresh unblocks the close."""
    runner._position_dirty[DirectionType.LONG] = True  # a prior 110017 dirtied it
    runner._long_position.size = Decimal("0.05")  # stale-low
    mock_rest_client.get_positions.return_value = _positions("0.2")  # actual high
    mock_executor.execute_place.return_value = OrderResult(success=True, order_id="ok")

    runner._execute_place_intent(_reduce_only_sell(qty="0.1"), EMPTY_LIMITS)

    assert runner._long_position.size == Decimal("0.2")
    assert mock_executor.execute_place.call_count == 1
    submitted = mock_executor.execute_place.call_args.args[0]
    assert submitted.qty == Decimal("0.1")  # unchanged qty — no cap


def test_dirty_rest_refresh_rate_limited(runner, mock_rest_client, clock):
    """Within the throttle window, only one REST refresh fires."""
    clock["now"] = 5.0
    runner._position_dirty[DirectionType.LONG] = True
    runner._long_position.size = Decimal("0.2")
    mock_rest_client.get_positions.return_value = _positions("0.05")

    intent = _reduce_only_sell(qty="0.1")
    for _ in range(5):
        runner._execute_place_intent(replace(intent), EMPTY_LIMITS)

    assert mock_rest_client.get_positions.call_count == 1


def test_refresh_position_size_updates_local_mirror(runner, mock_rest_client):
    mock_rest_client.get_positions.return_value = _positions("0.05", position_idx=1)
    runner._refresh_position_size_from_rest(DirectionType.LONG)
    assert runner._long_position.size == Decimal("0.05")
    assert runner._last_rest_position_size[DirectionType.LONG] == Decimal("0.05")


def test_refresh_position_size_short_side(runner, mock_rest_client):
    """P3.2: short-side refresh filters positionIdx==2 (parity with long)."""
    mock_rest_client.get_positions.return_value = _positions("0.07", position_idx=2)
    runner._refresh_position_size_from_rest(DirectionType.SHORT)
    assert runner._short_position.size == Decimal("0.07")
    assert runner._last_rest_position_size[DirectionType.SHORT] == Decimal("0.07")


def test_refresh_position_size_handles_flat_position(runner, mock_rest_client):
    # No matching positionIdx entry → flat
    mock_rest_client.get_positions.return_value = []
    runner._long_position.size = Decimal("0.2")
    runner._refresh_position_size_from_rest(DirectionType.LONG)
    assert runner._long_position.size == Decimal("0")
    # size="0" entry → flat
    mock_rest_client.get_positions.return_value = _positions("0", position_idx=1)
    runner._short_position.size = Decimal("0.2")
    runner._refresh_position_size_from_rest(DirectionType.SHORT)
    assert runner._short_position.size == Decimal("0")


def test_refresh_position_size_force_true_resets_dirty_state(runner, mock_rest_client):
    """force=True (forced reconcile) clears dirty + resets the throttle sentinel.

    Validates the state mutations on a REAL runner (the orchestrator test only
    asserts the call is made with force=True against a mocked runner).
    """
    runner._position_dirty[DirectionType.LONG] = True
    runner._last_dirty_rest_at[DirectionType.LONG] = 5.0
    runner._last_rest_position_size[DirectionType.LONG] = Decimal("0.2")
    mock_rest_client.get_positions.return_value = _positions("0.05")

    runner._refresh_position_size_from_rest(DirectionType.LONG, force=True)

    assert runner._long_position.size == Decimal("0.05")  # mirror written
    # force clears the whole episode (F3): baseline + throttle reset to None so
    # the prior episode's state never leaks into the next.
    assert runner._position_dirty[DirectionType.LONG] is False
    assert runner._last_dirty_rest_at[DirectionType.LONG] is None
    assert runner._last_rest_position_size[DirectionType.LONG] is None


def test_refresh_position_size_non_force_does_not_clear_dirty(runner, mock_rest_client, clock):
    """Non-force dirty refresh updates throttle ts but leaves dirty set."""
    clock["now"] = 7.0
    runner._position_dirty[DirectionType.LONG] = True
    mock_rest_client.get_positions.return_value = _positions("0.05")

    runner._refresh_position_size_from_rest(DirectionType.LONG)

    assert runner._position_dirty[DirectionType.LONG] is True
    assert runner._last_dirty_rest_at[DirectionType.LONG] == 7.0


def test_refresh_position_size_skips_malformed_position_idx(runner, mock_rest_client):
    """A malformed positionIdx entry is skipped, not crashed on (degrades)."""
    mock_rest_client.get_positions.return_value = [
        {"positionIdx": "garbage", "size": "9.9"},  # unparseable idx → skipped
        {"positionIdx": 1, "size": "0.05"},          # valid long entry
    ]
    runner._refresh_position_size_from_rest(DirectionType.LONG)
    assert runner._long_position.size == Decimal("0.05")


def test_refresh_position_size_no_rest_client_falls_back(
    strategy_config, mock_executor, instrument_info, clock, caplog
):
    r = StrategyRunner(
        strategy_config=strategy_config,
        executor=mock_executor,
        instrument_info=instrument_info,
        rest_client=None,
        clock=lambda: clock["now"],
    )
    r._long_position.size = Decimal("0.2")
    with caplog.at_level("WARNING"):
        r._refresh_position_size_from_rest(DirectionType.LONG)
    assert r._long_position.size == Decimal("0.2")  # unchanged
    assert any("rest" in rec.message.lower() for rec in caplog.records)


# --------------------------------------------------------------------------
# Phase 3 — circuit-breaker wiring in the runner
# --------------------------------------------------------------------------

def test_110017_retry_uses_fresh_order_link_id(
    strategy_config, mock_executor, instrument_info, mock_rest_client, clock, monkeypatch
):
    """Backstop: after a 110017 the next emission mints a FRESH wire id.

    Contrast with feature 0032: a non-110017 failure still reuses the id.
    """
    minted: list[str] = []

    def fake_make(client_order_id):
        link = f"{client_order_id}-mint-{len(minted)}"
        minted.append(link)
        return link

    monkeypatch.setattr("gridbot.runner.make_order_link_id", fake_make)
    cfg = strategy_config.model_copy(update={"dirty_refresh_enabled": False})
    r = StrategyRunner(
        strategy_config=cfg,
        executor=mock_executor,
        instrument_info=instrument_info,
        rest_client=mock_rest_client,
        clock=lambda: clock["now"],
    )
    r._wallet_balance = Decimal("10000")
    r._long_position.size = Decimal("0.2")  # stale → guard keeps passing
    mock_executor.execute_place.return_value = OrderResult(success=False, error=TRUNCATE_ERROR)

    intent = _reduce_only_sell(qty="0.1")
    r._execute_place_intent(replace(intent), EMPTY_LIMITS)
    first = mock_executor.execute_place.call_args.args[0]
    r._execute_place_intent(replace(intent), EMPTY_LIMITS)
    second = mock_executor.execute_place.call_args.args[0]

    assert second.order_link_id != first.order_link_id  # fresh id, not reused


def test_non_110017_failure_still_reuses_wire_id_and_enqueues(
    strategy_config, mock_executor, instrument_info, mock_rest_client, clock, monkeypatch
):
    """Guardrail: a network failure keeps feature-0032 reuse + retry-queue enqueue."""
    minted: list[str] = []

    def fake_make(client_order_id):
        link = f"{client_order_id}-mint-{len(minted)}"
        minted.append(link)
        return link

    monkeypatch.setattr("gridbot.runner.make_order_link_id", fake_make)
    r = StrategyRunner(
        strategy_config=strategy_config,
        executor=mock_executor,
        instrument_info=instrument_info,
        rest_client=mock_rest_client,
        clock=lambda: clock["now"],
    )
    r._wallet_balance = Decimal("10000")
    enqueued = []
    r._on_intent_failed = lambda intent, error: enqueued.append((intent, error))
    mock_executor.execute_place.return_value = OrderResult(success=False, error="Connection timeout")

    intent = PlaceLimitIntent.create(
        symbol="SOLUSDT", side="Buy", price=Decimal("84.0"),
        qty=Decimal("0.1"), grid_level=3, direction="long", reduce_only=False,
    )
    r._execute_place_intent(replace(intent), EMPTY_LIMITS)
    first = mock_executor.execute_place.call_args.args[0]
    r._execute_place_intent(replace(intent), EMPTY_LIMITS)
    second = mock_executor.execute_place.call_args.args[0]

    assert second.order_link_id == first.order_link_id  # reuse preserved
    assert len(enqueued) == 2  # both enqueued to retry queue


def test_breaker_blocks_before_refresh_on_tripped_scope(runner, mock_executor, mock_rest_client):
    """A tripped scope drops the intent at is_blocked — no REST refresh."""
    mock_executor.execute_place.return_value = OrderResult(success=False, error=TRUNCATE_ERROR)
    runner._long_position.size = Decimal("0.2")
    # Pre-trip the breaker on this scope key
    runner._truncate_breaker._tripped_until[("Sell", Decimal("84.51"))] = 999.0
    runner._position_dirty[DirectionType.LONG] = True

    runner._execute_place_intent(_reduce_only_sell(qty="0.1"), EMPTY_LIMITS)

    assert mock_executor.execute_place.call_count == 0
    assert mock_rest_client.get_positions.call_count == 0  # blocked before refresh


def test_successful_open_does_not_clear_dirty(runner, mock_executor):
    """F1: a successful OPEN (Buy, not reduce-only) must NOT clear dirty.

    Opens always pass the guard and prove nothing about position-size
    divergence; clearing dirty here would re-arm a 110017 on the next close.
    """
    runner._position_dirty[DirectionType.LONG] = True
    runner._last_rest_position_size[DirectionType.LONG] = Decimal("0.5")
    mock_executor.execute_place.return_value = OrderResult(success=True, order_id="ok")
    open_intent = PlaceLimitIntent.create(
        symbol="SOLUSDT", side="Buy", price=Decimal("80.0"),
        qty=Decimal("0.1"), grid_level=2, direction="long", reduce_only=False,
    )
    runner._execute_place_intent(open_intent, EMPTY_LIMITS)
    assert mock_executor.execute_place.call_count == 1  # open was placed
    assert runner._position_dirty[DirectionType.LONG] is True  # NOT cleared


def test_dirty_refresh_throttle_armed_on_failed_refresh(runner, mock_rest_client, clock):
    """F2: a failed refresh still arms the throttle (sentinel leaves None)."""
    clock["now"] = 3.0
    runner._position_dirty[DirectionType.LONG] = True
    mock_rest_client.get_positions.side_effect = Exception("REST down")
    runner._refresh_position_size_from_rest(DirectionType.LONG)
    assert runner._last_dirty_rest_at[DirectionType.LONG] == 3.0  # stamped despite failure


def test_failed_refresh_does_not_refire_every_tick(runner, mock_executor, mock_rest_client, clock):
    """F2 regression: persistent get_positions failure + guard-reject must not
    re-fire get_positions every tick (the unbounded case where the breaker
    never trips because nothing is submitted)."""
    clock["now"] = 100.0
    runner._position_dirty[DirectionType.LONG] = True
    runner._long_position.size = Decimal("0.05")  # stale-low → guard rejects
    mock_rest_client.get_positions.side_effect = Exception("REST down")

    intent = _reduce_only_sell(qty="0.1")
    for _ in range(5):
        runner._execute_place_intent(replace(intent), EMPTY_LIMITS)

    assert mock_rest_client.get_positions.call_count == 1  # throttled after first attempt
    assert mock_executor.execute_place.call_count == 0  # guard rejected, nothing submitted


def test_successful_close_resets_rest_baseline(runner, mock_executor, mock_rest_client):
    """F3: clearing dirty on a successful close resets the REST baseline."""
    runner._position_dirty[DirectionType.LONG] = True
    runner._last_dirty_rest_at[DirectionType.LONG] = None
    mock_rest_client.get_positions.return_value = _positions("0.5")  # ample
    mock_executor.execute_place.return_value = OrderResult(success=True, order_id="ok")

    runner._execute_place_intent(_reduce_only_sell(qty="0.1"), EMPTY_LIMITS)

    assert runner._position_dirty[DirectionType.LONG] is False
    assert runner._last_rest_position_size[DirectionType.LONG] is None  # baseline reset
    assert runner._last_dirty_rest_at[DirectionType.LONG] is None


def test_successful_place_clears_dirty(runner, mock_executor, mock_rest_client):
    runner._position_dirty[DirectionType.LONG] = True
    runner._long_position.size = Decimal("0.5")
    # REST agrees the position is large → pre-guard refresh keeps mirror 0.5,
    # guard passes (0.5 > 0.1), place succeeds → dirty cleared.
    mock_rest_client.get_positions.return_value = _positions("0.5")
    mock_executor.execute_place.return_value = OrderResult(success=True, order_id="ok")
    runner._execute_place_intent(_reduce_only_sell(qty="0.1"), EMPTY_LIMITS)
    assert runner._position_dirty[DirectionType.LONG] is False
    assert mock_executor.execute_place.call_count == 1


# --------------------------------------------------------------------------
# Phase 3 — dirty-window WS gate + WS-match clear (on_position_update)
# --------------------------------------------------------------------------

def _ws_pos(size):
    return {
        "size": str(size),
        "avgPrice": "84.0",
        "liqPrice": "0",
        "unrealisedPnl": "0",
        "cumRealisedPnl": "0",
        "curRealisedPnl": "0",
    }


def test_ws_match_clears_dirty(runner):
    runner._position_dirty[DirectionType.LONG] = True
    runner._last_rest_position_size[DirectionType.LONG] = Decimal("0.05")
    runner._last_dirty_rest_at[DirectionType.LONG] = 5.0

    runner.on_position_update(_ws_pos("0.05"), None, 10000.0, 84.0)
    assert runner._position_dirty[DirectionType.LONG] is False
    assert runner._last_dirty_rest_at[DirectionType.LONG] is None
    assert runner._last_rest_position_size[DirectionType.LONG] is None  # F3: baseline reset


def test_ws_nonmatch_keeps_dirty(runner):
    runner._position_dirty[DirectionType.LONG] = True
    runner._last_rest_position_size[DirectionType.LONG] = Decimal("0.05")

    runner.on_position_update(_ws_pos("0.07"), None, 10000.0, 84.0)
    assert runner._position_dirty[DirectionType.LONG] is True
    assert runner._long_position.size == Decimal("0.05")  # WS ignored, REST authoritative


def test_stale_ws_after_rest_refresh_does_not_reopen_storm(runner, mock_executor, mock_rest_client, clock):
    """Dangerous interleaving: a stale WS frame must not reopen the storm."""
    clock["now"] = 0.0
    runner._position_dirty[DirectionType.LONG] = True
    mock_rest_client.get_positions.return_value = _positions("0.05")
    runner._refresh_position_size_from_rest(DirectionType.LONG)  # mirror → 0.05, baseline 0.05
    assert runner._long_position.size == Decimal("0.05")

    # Stale WS frame says 0.2 (≠ last REST) → must be ignored
    runner.on_position_update(_ws_pos("0.2"), None, 10000.0, 84.0)
    assert runner._long_position.size == Decimal("0.05")
    assert runner._position_dirty[DirectionType.LONG] is True

    # Re-emit reduce-only within throttle → guard rejects, no submit
    mock_executor.execute_place.return_value = OrderResult(success=False, error=TRUNCATE_ERROR)
    runner._execute_place_intent(_reduce_only_sell(qty="0.1"), EMPTY_LIMITS)
    assert mock_executor.execute_place.call_count == 0


def test_dirty_ws_gate_inactive_without_rest_baseline(runner):
    """No REST baseline → the gate must NOT zero/restore; WS passes through."""
    runner._position_dirty[DirectionType.LONG] = True
    runner._last_rest_position_size[DirectionType.LONG] = None  # no refresh ran

    runner.on_position_update(_ws_pos("0.05"), None, 10000.0, 84.0)
    assert runner._long_position.size == Decimal("0.05")  # legit WS size written


# --------------------------------------------------------------------------
# Review v3 #1/#2 — observability metrics
# --------------------------------------------------------------------------

def test_rest_refresh_failure_count_increments_on_get_positions_error(runner, mock_rest_client):
    """#1: a get_positions failure during dirty refresh increments the metric."""
    runner._position_dirty[DirectionType.LONG] = True
    mock_rest_client.get_positions.side_effect = Exception("REST down")
    runner._refresh_position_size_from_rest(DirectionType.LONG)
    assert runner.dirty_rest_refresh_failure_count == 1
    runner._refresh_position_size_from_rest(DirectionType.LONG)
    assert runner.dirty_rest_refresh_failure_count == 2


def test_rest_refresh_failure_count_increments_on_unparseable_size(runner, mock_rest_client):
    """#1: an unparseable size also counts as a refresh failure."""
    mock_rest_client.get_positions.return_value = [{"positionIdx": 1, "size": "not-a-number"}]
    runner._refresh_position_size_from_rest(DirectionType.LONG)
    assert runner.dirty_rest_refresh_failure_count == 1


def test_rest_refresh_failure_count_not_incremented_on_success(runner, mock_rest_client):
    mock_rest_client.get_positions.return_value = _positions("0.05")
    runner._refresh_position_size_from_rest(DirectionType.LONG)
    assert runner.dirty_rest_refresh_failure_count == 0


def test_no_rest_client_does_not_increment_failure_count(
    strategy_config, mock_executor, instrument_info, clock
):
    """#1: rest_client=None is a static dry-run state, not a REST failure."""
    r = StrategyRunner(
        strategy_config=strategy_config, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None, clock=lambda: clock["now"],
    )
    r._refresh_position_size_from_rest(DirectionType.LONG)
    assert r.dirty_rest_refresh_failure_count == 0


def test_ws_mismatch_streak_increments_while_dirty(runner):
    """#2: consecutive non-matching WS frames during dirty increment the streak."""
    runner._position_dirty[DirectionType.LONG] = True
    runner._last_rest_position_size[DirectionType.LONG] = Decimal("0.05")
    for _ in range(3):
        runner.on_position_update(_ws_pos("0.07"), None, 10000.0, 84.0)
    assert runner._dirty_ws_mismatch_streak[DirectionType.LONG] == 3
    assert runner._position_dirty[DirectionType.LONG] is True  # still dirty


def test_ws_match_resets_mismatch_streak(runner):
    """#2: a matching WS frame ends the episode and resets the streak."""
    runner._position_dirty[DirectionType.LONG] = True
    runner._last_rest_position_size[DirectionType.LONG] = Decimal("0.05")
    runner.on_position_update(_ws_pos("0.07"), None, 10000.0, 84.0)  # mismatch → 1
    assert runner._dirty_ws_mismatch_streak[DirectionType.LONG] == 1
    runner.on_position_update(_ws_pos("0.05"), None, 10000.0, 84.0)  # match → clear + reset
    assert runner._dirty_ws_mismatch_streak[DirectionType.LONG] == 0
    assert runner._position_dirty[DirectionType.LONG] is False


def test_ws_mismatch_warns_at_threshold(runner, monkeypatch, caplog):
    """#2: a WARNING fires once the mismatch streak reaches the threshold."""
    monkeypatch.setattr("gridbot.runner._DIRTY_WS_MISMATCH_ALERT_THRESHOLD", 3)
    runner._position_dirty[DirectionType.LONG] = True
    runner._last_rest_position_size[DirectionType.LONG] = Decimal("0.05")
    with caplog.at_level("WARNING"):
        for _ in range(3):
            runner.on_position_update(_ws_pos("0.07"), None, 10000.0, 84.0)
    assert any("consecutive WS" in rec.getMessage() for rec in caplog.records)
