"""Feature 0066 — 110007 'available balance not enough' storm fix (issue #159).

Reproducing test + Phase 2 preflight/guard suite. The bot stormed because a
grown open-short (risk-management rule, mult=2.0) exceeded available balance →
Bybit 110007 on every submit → each failure enqueued to the retry queue.

The Phase 2 fix is a preflight balance check in ``_is_good_to_place`` (open
orders only; reduce-only bypasses) plus a retry-queue 110007 no-enqueue guard
in ``_execute_place_intent`` (mirrors the 0064 'do NOT enqueue 110017'
decision). These tests exercise ``_execute_place_intent`` / ``_is_good_to_place``
directly, the same way ``test_runner_truncate_storm.py`` does.
"""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock, MagicMock

import pytest

from gridcore import InstrumentInfo
from gridcore.intents import CancelIntent, PlaceLimitIntent
from gridcore.position import DirectionType

from gridbot.config import StrategyConfig
from gridbot.executor import IntentExecutor, OrderResult
from gridbot.position_fetcher import WalletSnapshot
from gridbot.runner import StrategyRunner, TrackedOrder

EMPTY_LIMITS: dict[str, list[dict]] = {"long": [], "short": []}

# Bybit 110007 wire format (matches executor._ERR_CODE_RE: `[NNNNN]`).
INSUFFICIENT_BALANCE_ERROR = (
    "Bybit API error in place_order: [110007] ab not enough for new order"
)


def _open_short(price="100.0", qty="1.0"):
    """A grown open-short Sell (the order that stormed): reduce_only=False."""
    return PlaceLimitIntent.create(
        symbol="SOLUSDT",
        side="Sell",
        price=Decimal(price),
        qty=Decimal(qty),
        grid_level=3,
        direction="short",
        reduce_only=False,
    )


@pytest.fixture
def strategy_config():
    return StrategyConfig(
        strat_id="solusdt_test",
        account="test_account",
        symbol="SOLUSDT",
        tick_size=Decimal("0.01"),
        grid_count=30,
        grid_step=0.3,
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
def runner(strategy_config, mock_executor, instrument_info):
    r = StrategyRunner(
        strategy_config=strategy_config,
        executor=mock_executor,
        instrument_info=instrument_info,
        rest_client=None,
        clock=lambda: 0.0,
    )
    r._wallet_balance = Decimal("10000")
    return r


# --------------------------------------------------------------------------
# Reproducing test — must FAIL before Phase 2, PASS after.
# --------------------------------------------------------------------------

def test_110007_storm_blocked_by_preflight(runner, mock_executor):
    """Low-balance + grown open-short → preflight rejects locally.

    Pre-fix: the open is submitted, Bybit returns 110007, the failure is
    enqueued to the retry queue (the storm). Post-fix: ``_is_good_to_place``
    rejects the open before submit → zero placements, zero retry-queue adds.
    """
    enqueued = []
    runner._on_intent_failed = lambda intent, error: enqueued.append((intent, error))

    # est_cost = qty*price/leverage = 1.0*100/1.0 = 100 ≫ available 5.
    runner._available_balance = Decimal("5")
    mock_executor.execute_place.return_value = OrderResult(
        success=False, error=INSUFFICIENT_BALANCE_ERROR
    )

    intent = _open_short(price="100.0", qty="1.0")
    for _ in range(5):  # per-tick re-emission
        runner._execute_place_intent(replace(intent), EMPTY_LIMITS)

    assert mock_executor.execute_place.call_count == 0  # never submitted
    assert enqueued == []  # never enqueued to the retry queue


# --------------------------------------------------------------------------
# Phase 1 — observability data layer
# --------------------------------------------------------------------------

def _long_pos(size="1.0", **extra):
    pos = {
        "size": size, "avgPrice": "100", "liqPrice": "80",
        "unrealisedPnl": "0", "cumRealisedPnl": "0", "curRealisedPnl": "0",
    }
    pos.update(extra)
    return pos


def test_build_position_state_carries_im_mm_and_leverage(runner):
    state = runner._build_position_state(
        _long_pos(size="1.5", positionIM="150.5", positionMM="7.25", leverage="10"),
        10000.0,
        DirectionType.LONG,
    )
    assert state.initial_margin == Decimal("150.5")
    assert state.maintenance_margin == Decimal("7.25")
    # Live leverage captured for the preflight — but NOT onto PositionState.leverage
    # (which must keep its default to preserve the risk-multiplier upnl calc).
    assert runner._leverage[DirectionType.LONG] == 10.0
    assert state.leverage == 1


def test_build_position_state_missing_im_mm_defaults_zero(runner):
    state = runner._build_position_state(_long_pos(size="1.0"), 10000.0, DirectionType.LONG)
    assert state.initial_margin == Decimal("0")
    assert state.maintenance_margin == Decimal("0")


def test_position_update_log_has_balance_tokens(runner, caplog):
    with caplog.at_level("INFO"):
        runner.on_position_update(
            _long_pos(size="1.0"), None, 10000.0, 100.0,
            available_balance=1234.5,
            total_available_balance=2000.0,
            total_maintenance_margin=15.0,
        )
    line = next(r.getMessage() for r in caplog.records
                if "Position update -" in r.getMessage())
    # Heartbeat shape preserved (existing tokens) + new ones appended.
    assert "ratio=" in line and "long_mult=" in line and "short_mult=" in line
    assert "avail=1234.50" in line
    assert "total_avail=2000.00" in line
    assert "total_mm=15.00" in line


def test_position_update_balance_defaults_preserve_state(runner):
    """Omitting the new kwargs (old callers) leaves stored balance untouched."""
    runner._available_balance = Decimal("500")
    runner.on_position_update(_long_pos(size="1.0"), None, 10000.0, 100.0)
    assert runner._available_balance == Decimal("500")


# --------------------------------------------------------------------------
# Phase 2 — preflight balance check (_is_good_to_place)
# --------------------------------------------------------------------------

def test_preflight_blocks_unaffordable_open(runner):
    runner._available_balance = Decimal("5")  # est_cost=100 ≫ 5
    assert runner._is_good_to_place(_open_short(price="100.0", qty="1.0"), EMPTY_LIMITS) is False


def test_preflight_allows_affordable_open(runner):
    runner._available_balance = Decimal("200")  # 200 ≥ 100*1.05
    assert runner._is_good_to_place(_open_short(price="100.0", qty="1.0"), EMPTY_LIMITS) is True


def test_preflight_bypassed_for_reduce_only(runner):
    """Reduce-only skips the preflight (frees margin) → only the size guard."""
    runner._available_balance = Decimal("1")  # would block an open
    runner._short_position.size = Decimal("5")
    close_short = PlaceLimitIntent.create(
        symbol="SOLUSDT", side="Buy", price=Decimal("100.0"), qty=Decimal("1.0"),
        grid_level=3, direction="short", reduce_only=True,
    )
    # Passes the size guard (5 > 1) and is NOT blocked by the low-balance preflight.
    assert runner._is_good_to_place(close_short, EMPTY_LIMITS) is True


def test_preflight_fail_open_when_no_balance_data(runner):
    """avail == 0 (no data yet) → skip the check, behave as before the fix."""
    runner._available_balance = Decimal("0")
    assert runner._is_good_to_place(_open_short(price="100.0", qty="1.0"), EMPTY_LIMITS) is True


def test_preflight_live_leverage_makes_open_affordable(runner):
    """est_cost = qty*price/leverage: live 10x leverage clears an open that 1x blocks."""
    runner._available_balance = Decimal("15")
    intent = _open_short(price="100.0", qty="1.0")  # notional 100
    # Default (assumed 1x): est_cost 100 > 15 → blocked.
    assert runner._is_good_to_place(intent, EMPTY_LIMITS) is False
    # Live 10x: est_cost 10, 15 ≥ 10*1.05 → allowed.
    runner._leverage["short"] = 10.0
    assert runner._is_good_to_place(intent, EMPTY_LIMITS) is True


def test_preflight_buffer_boundary(runner):
    """At exactly est_cost the buffer still rejects; just above it passes."""
    intent = _open_short(price="100.0", qty="1.0")  # est_cost=100 at 1x
    runner._available_balance = Decimal("100")   # 100 < 100*1.05
    assert runner._is_good_to_place(intent, EMPTY_LIMITS) is False
    runner._available_balance = Decimal("106")   # 106 ≥ 105
    assert runner._is_good_to_place(intent, EMPTY_LIMITS) is True


def test_preflight_disabled_via_killswitch(strategy_config, mock_executor, instrument_info):
    cfg = strategy_config.model_copy(update={"preflight_balance_check_enabled": False})
    r = StrategyRunner(
        strategy_config=cfg, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None, clock=lambda: 0.0,
    )
    r._available_balance = Decimal("5")  # would block if enabled
    assert r._is_good_to_place(_open_short(price="100.0", qty="1.0"), EMPTY_LIMITS) is True


# --------------------------------------------------------------------------
# Phase 2 — retry-queue 110007 guard (_execute_place_intent)
# --------------------------------------------------------------------------

def test_110007_not_enqueued_to_retry_queue(runner, mock_executor):
    """Boundary race: a submitted open that 110007s is dropped, not enqueued."""
    enqueued = []
    runner._on_intent_failed = lambda intent, error: enqueued.append((intent, error))
    runner._available_balance = Decimal("0")  # fail-open so the order is submitted
    mock_executor.execute_place.return_value = OrderResult(
        success=False, error=INSUFFICIENT_BALANCE_ERROR
    )
    runner._execute_place_intent(_open_short(price="100.0", qty="1.0"), EMPTY_LIMITS)
    assert mock_executor.execute_place.call_count == 1  # it WAS submitted (race)
    assert enqueued == []  # but not enqueued


def test_non_110007_failure_still_enqueues(runner, mock_executor):
    """Guardrail: the 110007 drop must not swallow other failures."""
    enqueued = []
    runner._on_intent_failed = lambda intent, error: enqueued.append((intent, error))
    runner._available_balance = Decimal("0")  # fail-open so the order is submitted
    mock_executor.execute_place.return_value = OrderResult(
        success=False, error="Connection timeout"
    )
    runner._execute_place_intent(_open_short(price="100.0", qty="1.0"), EMPTY_LIMITS)
    assert len(enqueued) == 1  # ordinary failure still retried


# --------------------------------------------------------------------------
# Phase 3a — low-balance predicate + moderate_liq_risk fix wiring
# --------------------------------------------------------------------------

def test_low_balance_predicate(runner):
    # frac default 0.10: low-balance when avail < total_position_value*0.10.
    runner._available_balance = Decimal("5")
    assert runner._is_low_balance(Decimal("100")) is True   # 5 < 10
    runner._available_balance = Decimal("50")
    assert runner._is_low_balance(Decimal("100")) is False  # 50 ≥ 10
    runner._available_balance = Decimal("0")
    assert runner._is_low_balance(Decimal("100")) is False  # no data → False
    runner._available_balance = Decimal("5")
    assert runner._is_low_balance(Decimal("0")) is False    # no position → False


def test_on_position_update_sets_low_balance_flag(runner):
    # long size 1 @ entry 100 → position_value 100; avail 5 < 10 → low-balance.
    runner.on_position_update(_long_pos(size="1.0"), None, 10000.0, 100.0,
                              available_balance=5.0)
    assert runner._low_balance is True


def test_on_position_update_low_balance_false_when_ample(runner):
    runner.on_position_update(_long_pos(size="1.0"), None, 10000.0, 100.0,
                              available_balance=50.0)
    assert runner._low_balance is False


# --------------------------------------------------------------------------
# Phase 3b — chase-close active defense (default OFF)
# --------------------------------------------------------------------------

@pytest.fixture
def chase_runner(strategy_config, mock_executor, instrument_info):
    cfg = strategy_config.model_copy(update={
        "chase_close_enabled": True,
        "chase_position_ratio_threshold": 5.0,
        "chase_offset_pct": 0.001,
        "chase_replace_drift_pct": 0.001,
        "chase_close_hysteresis": 0.1,
    })
    r = StrategyRunner(
        strategy_config=cfg, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None, clock=lambda: 0.0,
    )
    r._wallet_balance = Decimal("10000")
    return r


def _places(buf):
    return [i for i in buf if isinstance(i, PlaceLimitIntent)]


def _cancels(buf):
    return [i for i in buf if isinstance(i, CancelIntent)]


def test_chase_disabled_by_default(runner):
    """Default config has chase_close_enabled=False → never enters."""
    runner._low_balance = True
    runner._long_position.size = Decimal("10")
    runner._evaluate_chase(price=100.0, position_ratio=8.0)
    assert runner._chase_state == "IDLE"
    assert runner._pending_chase_intents == []


def test_chase_enters_buffers_intents_only(chase_runner):
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(price=100.0, position_ratio=8.0)  # long dominant
    assert chase_runner._chase_state == "CHASING"
    assert chase_runner._chase_direction == "long"
    places = _places(chase_runner._pending_chase_intents)
    assert len(places) == 1
    assert places[0].reduce_only is True and places[0].post_only is True
    assert places[0].side == "Sell"  # close a long


def test_chase_places_reduce_only_post_only_at_maker_offset(chase_runner):
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 8.0)
    place = _places(chase_runner._pending_chase_intents)[0]
    assert place.price == Decimal("100.10")  # 100*(1+0.001), maker-safe above
    assert place.qty == Decimal("5")          # half of 10, < position size


def test_chase_short_dominant_buys_below(chase_runner):
    chase_runner._low_balance = True
    chase_runner._short_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 0.1)  # short dominant (ratio < 1/5)
    place = _places(chase_runner._pending_chase_intents)[0]
    assert place.side == "Buy"               # close a short
    assert place.price == Decimal("99.90")   # 100*(1-0.001), maker-safe below
    assert chase_runner._chase_direction == "short"


def test_chase_cancels_grow_side_orders_on_entry(chase_runner):
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    open_buy = PlaceLimitIntent.create(
        symbol="SOLUSDT", side="Buy", price=Decimal("95"), qty=Decimal("1"),
        grid_level=2, direction="long", reduce_only=False,
    )
    chase_runner._tracked_orders[open_buy.client_order_id] = TrackedOrder(
        client_order_id=open_buy.client_order_id, intent=open_buy,
        status="placed", order_id="oid-1",
    )
    chase_runner._evaluate_chase(100.0, 8.0)
    assert any(c.order_id == "oid-1" for c in _cancels(chase_runner._pending_chase_intents))


def test_chase_intents_drained_dispatches_and_clears(chase_runner, mock_executor):
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 8.0)
    assert chase_runner._pending_chase_intents  # buffered, not dispatched
    chase_runner._drain_pending_chase_intents()
    assert chase_runner._pending_chase_intents == []  # cleared
    assert mock_executor.execute_place.call_count == 1
    submitted = mock_executor.execute_place.call_args.args[0]
    assert submitted.reduce_only is True and submitted.post_only is True


def test_chase_qty_stays_below_position_size(chase_runner):
    intent = chase_runner._build_chase_order("long", "Sell", 100.0, Decimal("10"))
    assert intent.qty < Decimal("10")  # never over-closes (reduce-only guard safe)


def test_chase_no_order_for_untrimmable_tiny_position(chase_runner):
    # size == one qty_step → half rounds back up to size → no safe trim.
    assert chase_runner._build_chase_order("long", "Sell", 100.0, Decimal("0.01")) is None


def test_chase_replaces_on_drift(chase_runner):
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 8.0)  # enter at ~100.10
    first_price = chase_runner._chase_order["price"]
    coid = chase_runner._chase_order["client_order_id"]
    chase_runner._tracked_orders[coid] = TrackedOrder(
        client_order_id=coid, intent=None, status="placed", order_id="chase-1",
    )
    chase_runner._pending_chase_intents.clear()  # ignore entry intents
    chase_runner._evaluate_chase(110.0, 8.0)  # price drifted ~10% → re-peg
    assert any(c.order_id == "chase-1" for c in _cancels(chase_runner._pending_chase_intents))
    assert len(_places(chase_runner._pending_chase_intents)) == 1
    assert chase_runner._chase_order["price"] != first_price


def test_chase_exits_on_balance_recovery(chase_runner):
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 8.0)
    assert chase_runner._chase_state == "CHASING"
    chase_runner._low_balance = False  # balance recovered
    chase_runner._evaluate_chase(100.0, 8.0)
    assert chase_runner._chase_state == "IDLE"


def test_chase_hysteresis_no_flap_then_exit(chase_runner):
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 8.0)
    # ratio 4.8 is below threshold 5 but above exit floor 5*(1-0.1)=4.5 → stay.
    chase_runner._evaluate_chase(100.0, 4.8)
    assert chase_runner._chase_state == "CHASING"
    # ratio 4.0 < 4.5 → exit.
    chase_runner._evaluate_chase(100.0, 4.0)
    assert chase_runner._chase_state == "IDLE"


def test_chase_exit_before_drain_does_not_orphan_buffered_place(chase_runner):
    """Exit before the buffered chase place is dispatched must drop it, not
    leave an orphaned resting reduce-only order."""
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 8.0)            # enter → place buffered
    assert len(_places(chase_runner._pending_chase_intents)) == 1
    chase_runner._low_balance = False                   # balance recovers
    chase_runner._evaluate_chase(100.0, 8.0)            # exit before any drain
    assert _places(chase_runner._pending_chase_intents) == []  # no orphan
    assert chase_runner._chase_state == "IDLE"


def test_chase_exit_before_drain_drops_buffered_grow_side_cancels(chase_runner):
    """Exit before drain must not leave buffered grow-side cancels that would
    fire on the next dispatch tick after chase mode has ended."""
    grow_buy = PlaceLimitIntent.create(
        symbol="SOLUSDT", side="Buy", price=Decimal("95"), qty=Decimal("1"),
        grid_level=1, direction="long", reduce_only=False,
    )
    chase_runner._tracked_orders[grow_buy.client_order_id] = TrackedOrder(
        client_order_id=grow_buy.client_order_id, intent=grow_buy,
        status="placed", order_id="grow-1",
    )
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 8.0)  # enter → grow cancel + chase buffered
    assert any(c.order_id == "grow-1" for c in _cancels(chase_runner._pending_chase_intents))
    chase_runner._low_balance = False         # balance recovers before drain
    chase_runner._evaluate_chase(100.0, 8.0)  # exit
    assert _cancels(chase_runner._pending_chase_intents) == []
    assert _places(chase_runner._pending_chase_intents) == []
    assert chase_runner._chase_state == "IDLE"


def test_chase_repeg_before_drain_replaces_not_accumulates(chase_runner):
    """Re-peg before drain replaces the still-buffered place (no accumulation)."""
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 8.0)   # buffered place at 100.10
    chase_runner._evaluate_chase(110.0, 8.0)   # drift before drain → re-peg
    places = _places(chase_runner._pending_chase_intents)
    assert len(places) == 1                    # old buffered place replaced
    assert places[0].price == Decimal("110.11")


def test_chase_no_entry_when_position_too_small(chase_runner):
    """_build_chase_order returns None for an untrimmable size → stay IDLE."""
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("0.01")  # one qty_step
    chase_runner._evaluate_chase(100.0, 8.0)
    assert chase_runner._chase_state == "IDLE"
    assert chase_runner._pending_chase_intents == []


def test_chase_does_not_cancel_reduce_only_grow_side_orders(chase_runner):
    """Only non-reduce-only grow opens are cancelled on entry, not closes."""
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    close_sell = PlaceLimitIntent.create(
        symbol="SOLUSDT", side="Sell", price=Decimal("105"), qty=Decimal("1"),
        grid_level=2, direction="long", reduce_only=True,
    )
    chase_runner._tracked_orders[close_sell.client_order_id] = TrackedOrder(
        client_order_id=close_sell.client_order_id, intent=close_sell,
        status="placed", order_id="ro-1",
    )
    chase_runner._evaluate_chase(100.0, 8.0)
    assert not any(c.order_id == "ro-1" for c in _cancels(chase_runner._pending_chase_intents))


def test_chase_no_entry_at_exact_threshold(chase_runner):
    """Entry is strictly > threshold (and < 1/threshold), not ==."""
    chase_runner._low_balance = True
    chase_runner._long_position.size = Decimal("10")
    chase_runner._evaluate_chase(100.0, 5.0)  # exactly threshold → no entry
    assert chase_runner._chase_state == "IDLE"


# --------------------------------------------------------------------------
# Review F2 — moderate_liq_risk fix kill-switch wiring at the runner layer
# --------------------------------------------------------------------------

def test_moderate_liq_killswitch_off_keeps_throttle_under_low_balance(
    strategy_config, mock_executor, instrument_info
):
    """Kill-switch OFF → runner passes low_balance=False, throttle still applies."""
    cfg = strategy_config.model_copy(
        update={"moderate_liq_low_balance_fix_enabled": False})
    r = StrategyRunner(
        strategy_config=cfg, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None, clock=lambda: 0.0,
    )
    r._wallet_balance = Decimal("10000")
    # long liq_ratio = 82/100 = 0.82 → moderate_liq band; short present; low balance
    # (avail 50 < total_position_value 1100 * 0.10).
    r.on_position_update(_long_pos("10", liqPrice="82"), _long_pos("1", liqPrice="0"),
                         10000.0, 100.0, available_balance=50.0)
    assert r._low_balance is True
    assert r._short_position.get_amount_multiplier()["Buy"] == 0.5  # throttle kept


def test_moderate_liq_fix_on_skips_throttle_under_low_balance(runner):
    """Default kill-switch ON → runner passes low_balance=True, throttle skipped."""
    runner.on_position_update(_long_pos("10", liqPrice="82"), _long_pos("1", liqPrice="0"),
                             10000.0, 100.0, available_balance=50.0)
    assert runner._low_balance is True
    assert runner._short_position.get_amount_multiplier()["Buy"] == 1.0  # not throttled


# --------------------------------------------------------------------------
# Review F4 — 110007 drop is OPEN-only; reduce-only failures keep retry path
# --------------------------------------------------------------------------

def test_reduce_only_110007_still_enqueues(runner, mock_executor):
    enqueued = []
    runner._on_intent_failed = lambda intent, error: enqueued.append((intent, error))
    runner._short_position.size = Decimal("5")  # reduce-only guard passes (5 > 1)
    close_short = PlaceLimitIntent.create(
        symbol="SOLUSDT", side="Buy", price=Decimal("100.0"), qty=Decimal("1.0"),
        grid_level=3, direction="short", reduce_only=True,
    )
    mock_executor.execute_place.return_value = OrderResult(
        success=False, error=INSUFFICIENT_BALANCE_ERROR)
    runner._execute_place_intent(close_short, EMPTY_LIMITS)
    assert mock_executor.execute_place.call_count == 1  # reduce-only bypasses preflight
    assert len(enqueued) == 1  # NOT dropped — kept on the normal retry path


# --------------------------------------------------------------------------
# Review coverage note — dedicated assumed_leverage fallback test
# --------------------------------------------------------------------------

def test_preflight_assumed_leverage_fallback(
    runner, strategy_config, mock_executor, instrument_info
):
    intent = _open_short("100.0", "1.0")  # notional 100
    # Default assumed_leverage 1.0, no live leverage captured → est_cost 100 > 25.
    runner._available_balance = Decimal("25")
    assert runner._is_good_to_place(intent, EMPTY_LIMITS) is False
    # assumed_leverage 5 → est_cost 20; 25 >= 20*1.05 → allowed (fallback used).
    cfg = strategy_config.model_copy(update={"assumed_leverage": 5.0})
    r5 = StrategyRunner(
        strategy_config=cfg, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None, clock=lambda: 0.0,
    )
    r5._available_balance = Decimal("25")
    assert r5._is_good_to_place(intent, EMPTY_LIMITS) is True


# --------------------------------------------------------------------------
# Phase 4 — provider-fed preflight (fresh, non-blocking, fail-open)
# --------------------------------------------------------------------------

def _runner_with_provider(
    strategy_config, mock_executor, instrument_info, provider,
    max_age=45.0,
):
    return StrategyRunner(
        strategy_config=strategy_config, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None, clock=lambda: 0.0,
        wallet_provider=provider, wallet_ws_max_age_seconds=max_age,
    )


def test_preflight_uses_fresh_provider_value(
    strategy_config, mock_executor, instrument_info
):
    """A fresh LOW provider balance blocks even when _available_balance is stale-high."""
    provider = lambda: (WalletSnapshot(available_balance=5.0), 1.0)  # fresh, age 1s
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, provider)
    r._available_balance = Decimal("100000")  # stale-high — must be ignored
    # est_cost = 100 ≫ fresh 5 → blocked via the provider value, not the latch.
    assert r._is_good_to_place(_open_short("100.0", "1.0"), EMPTY_LIMITS) is False


def test_preflight_blocks_on_fresh_zero_provider(
    strategy_config, mock_executor, instrument_info
):
    """A FRESH peek of available_balance == 0 is authoritative → block (review P1).

    The fresh zero means 'no free margin', NOT 'no data'; it must block every
    open and must NOT be replaced by a stale-high _available_balance.
    """
    provider = lambda: (WalletSnapshot(available_balance=0.0), 1.0)  # fresh zero
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, provider)
    r._available_balance = Decimal("100000")  # stale-high — must not mask the zero
    assert r._is_good_to_place(_open_short("100.0", "1.0"), EMPTY_LIMITS) is False


def test_preflight_fails_open_when_peek_stale(
    strategy_config, mock_executor, instrument_info
):
    """A STALE peek (age >= max_age) → fail-open; does NOT trust the stale peek
    and does NOT fall back to _available_balance in the provider path (review P1).

    Both the stale-peek value and _available_balance are set LOW (would block if
    used); the open still passes, proving the check was skipped entirely.
    """
    provider = lambda: (WalletSnapshot(available_balance=5.0), 100.0)  # stale (>=45)
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, provider)
    r._available_balance = Decimal("5")  # also low — must NOT be used as fallback
    assert r._is_good_to_place(_open_short("100.0", "1.0"), EMPTY_LIMITS) is True


def test_preflight_fails_open_when_peek_none(
    strategy_config, mock_executor, instrument_info
):
    """peek None (no WS, no REST cache) in the provider path → fail-open, no fallback."""
    provider = lambda: None
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, provider)
    r._available_balance = Decimal("5")  # low — must NOT be used as fallback
    assert r._is_good_to_place(_open_short("100.0", "1.0"), EMPTY_LIMITS) is True


def test_preflight_provider_exception_fails_open(
    strategy_config, mock_executor, instrument_info
):
    """Provider raising must be caught (never abort dispatch) → fail-open."""
    def provider():
        raise RuntimeError("boom")
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, provider)
    r._available_balance = Decimal("5")  # low — must NOT be used as fallback
    assert r._is_good_to_place(_open_short("100.0", "1.0"), EMPTY_LIMITS) is True


def test_preflight_fresh_provider_allows_affordable_open(
    strategy_config, mock_executor, instrument_info
):
    """A fresh ample provider balance allows the open."""
    provider = lambda: (WalletSnapshot(available_balance=200.0), 1.0)
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, provider)
    r._available_balance = Decimal("0")
    assert r._is_good_to_place(_open_short("100.0", "1.0"), EMPTY_LIMITS) is True


def test_preflight_no_provider_uses_available_balance(runner):
    """No provider wired (legacy / wallet_ws_enabled=False) → _available_balance
    path with the '>0 means data' rule (Phase-2 behavior preserved)."""
    assert runner._wallet_provider is None
    runner._available_balance = Decimal("5")  # est_cost 100 ≫ 5 → blocked
    assert runner._is_good_to_place(_open_short("100.0", "1.0"), EMPTY_LIMITS) is False
    runner._available_balance = Decimal("0")  # no data → fail-open
    assert runner._is_good_to_place(_open_short("100.0", "1.0"), EMPTY_LIMITS) is True


def test_storm_blocked_end_to_end_via_provider(
    strategy_config, mock_executor, instrument_info
):
    """Production-path regression guard: a FRESH-LOW provider blocks the storm
    end-to-end through ``_execute_place_intent`` → ``_is_good_to_place`` (the path
    that ships with ``wallet_ws_enabled=True``), not only the legacy
    ``_available_balance`` latch (covered by test_110007_storm_blocked_by_preflight).
    """
    provider = lambda: (WalletSnapshot(available_balance=5.0), 1.0)  # fresh, low
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, provider)
    enqueued = []
    r._on_intent_failed = lambda intent, error: enqueued.append((intent, error))
    r._available_balance = Decimal("100000")  # stale-high latch must NOT rescue the open
    mock_executor.execute_place.return_value = OrderResult(
        success=False, error=INSUFFICIENT_BALANCE_ERROR
    )
    intent = _open_short(price="100.0", qty="1.0")
    for _ in range(5):  # per-tick re-emission
        r._execute_place_intent(replace(intent), EMPTY_LIMITS)
    assert mock_executor.execute_place.call_count == 0  # never submitted
    assert enqueued == []  # never enqueued to the retry queue


# --------------------------------------------------------------------------
# Review F1 — low-balance PREDICATE shares the provider freshness with the
# preflight (closes the chase/moderate_liq lag; plan rollout §3 prerequisite)
# --------------------------------------------------------------------------

def test_low_balance_predicate_uses_fresh_provider(
    strategy_config, mock_executor, instrument_info
):
    """A FRESH provider value drives the predicate even when the latch is stale-high."""
    provider = lambda: (WalletSnapshot(available_balance=5.0), 1.0)  # fresh, low
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, provider)
    r._available_balance = Decimal("100000")  # stale-high latch must be ignored
    assert r._is_low_balance(Decimal("100")) is True  # 5 < 100*0.10


def test_low_balance_predicate_fresh_zero_is_low_balance(
    strategy_config, mock_executor, instrument_info
):
    """A FRESH provider zero = genuinely no free margin = the MOST extreme
    low-balance state → True (NOT treated as 'no data' like a latch 0)."""
    provider = lambda: (WalletSnapshot(available_balance=0.0), 1.0)  # fresh zero
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, provider)
    r._available_balance = Decimal("100000")  # stale-high latch must not mask it
    assert r._is_low_balance(Decimal("100")) is True


def test_low_balance_predicate_stale_provider_falls_back_to_latch(
    strategy_config, mock_executor, instrument_info
):
    """A stale/None/raising provider falls back to the position-cadence latch
    (best-available), unlike the preflight which fails open."""
    stale = lambda: (WalletSnapshot(available_balance=1.0), 100.0)  # stale (>=45)
    r = _runner_with_provider(strategy_config, mock_executor, instrument_info, stale)
    r._available_balance = Decimal("50")  # latch ample → not low (50 >= 10)
    assert r._is_low_balance(Decimal("100")) is False

    def _raise():
        raise RuntimeError("boom")
    r2 = _runner_with_provider(strategy_config, mock_executor, instrument_info, _raise)
    r2._available_balance = Decimal("5")  # latch low → low (5 < 10), no raise escapes
    assert r2._is_low_balance(Decimal("100")) is True


# --------------------------------------------------------------------------
# Feature 0067 — suppress LowBalanceSkip log spam (issue #164)
#
# Two complementary, default-on, kill-switchable changes: (1) state-transition
# (ENTER/EXIT) INFO logging that suppresses the per-intent DEBUG spam while the
# sustained-skip regime is active for a (direction, side) key; (2) a periodic
# 60s INFO summary. Edges resolve at the SAMPLE boundary (Part B
# `_reconcile_skip_edges`), not inline per intent — so the unit shape is
# "preflight per intent, then reconcile", and ≥1 integration test drives the
# real dispatch handlers (reconcile runs at the top of the NEXT dispatch).
# --------------------------------------------------------------------------

class _FakeClock:
    """Mutable monotonic float-seconds clock for the 60s-window / idle-timeout
    tests (injected as ``clock=`` like the existing timing tests)."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _open_long(price="100.0", qty="1.0"):
    """A grow-side open-long Buy (reduce_only=False) → key ('long', 'Buy')."""
    return PlaceLimitIntent.create(
        symbol="SOLUSDT", side="Buy", price=Decimal(price), qty=Decimal(qty),
        grid_level=3, direction="long", reduce_only=False,
    )


def _skip_debug_lines(caplog):
    """The per-intent ``LowBalanceSkip`` DEBUG lines only (NOT the 'preflight
    skipped (no fresh balance data)' fail-open DEBUG, NOT INFO edges/summary)."""
    return [r for r in caplog.records
            if r.levelname == "DEBUG" and "LowBalanceSkip" in r.getMessage()]


def _info_lines(caplog, token):
    return [r for r in caplog.records
            if r.levelname == "INFO" and token in r.getMessage()]


@pytest.fixture
def lb_clock():
    return _FakeClock()


@pytest.fixture
def lb_runner(strategy_config, mock_executor, instrument_info, lb_clock):
    """Runner on a mutable clock, low (blocking) available balance, no provider
    → open preflights reach the LowBalanceSkip site. Feature 0067 transition +
    summary default-on. (Sets ``_available_balance`` — NOT ``_wallet_balance``,
    which is unrelated to the preflight; runner.py:1409.)"""
    r = StrategyRunner(
        strategy_config=strategy_config, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None, clock=lb_clock,
    )
    r._available_balance = Decimal("5")  # est_cost 100 ≫ 5 → blocked (no provider)
    return r


def _make_runner(strategy_config, mock_executor, instrument_info, clock, **cfg_update):
    cfg = strategy_config.model_copy(update=cfg_update) if cfg_update else strategy_config
    r = StrategyRunner(
        strategy_config=cfg, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None, clock=clock,
    )
    r._available_balance = Decimal("5")
    return r


# ---- Phase 1 — state-transition (ENTER/EXIT) logging ----

def test_low_balance_skip_logs_only_on_transition(lb_runner, caplog):
    """Same unaffordable key over two samples → exactly 1 ENTER, 0 EXIT, and
    ZERO per-intent DEBUG (the edge carries the signal). Block decision still True."""
    with caplog.at_level("DEBUG"):
        assert lb_runner._preflight_blocks_open(_open_short("100.0", "1.0")) is True
        lb_runner._reconcile_skip_edges()
        assert lb_runner._preflight_blocks_open(_open_short("100.0", "1.0")) is True
        lb_runner._reconcile_skip_edges()
    enter = _info_lines(caplog, "LowBalanceSkip ENTER")
    assert len(enter) == 1
    # Guard the ENTER operator payload shape (a dropped/mis-formatted token would
    # otherwise pass every other test, which only checks the "ENTER" substring).
    enter_msg = enter[0].getMessage()
    assert "direction=short side=Sell" in enter_msg
    assert "first_blocked_price=" in enter_msg
    assert "avail_min=" in enter_msg and "avail_max=" in enter_msg
    assert len(_info_lines(caplog, "LowBalanceSkip EXIT")) == 0
    assert _skip_debug_lines(caplog) == []


def test_low_balance_skip_exit_after_balance_restored(lb_runner, caplog):
    """Sample 1 blocked (ENTER); sample 2 same key affordable (EXIT carrying count)."""
    with caplog.at_level("DEBUG"):
        lb_runner._available_balance = Decimal("5")
        lb_runner._preflight_blocks_open(_open_short("100.0", "1.0"))
        lb_runner._reconcile_skip_edges()  # ENTER
        lb_runner._available_balance = Decimal("200")  # affordable now
        assert lb_runner._preflight_blocks_open(_open_short("100.0", "1.0")) is False
        lb_runner._reconcile_skip_edges()  # EXIT
    assert len(_info_lines(caplog, "LowBalanceSkip ENTER")) == 1
    exits = _info_lines(caplog, "LowBalanceSkip EXIT")
    assert len(exits) == 1
    assert "after 1 skips" in exits[0].getMessage()
    assert lb_runner._skip_state[("short", "Sell")]["active"] is False


@pytest.mark.parametrize("affordable_first", [True, False])
def test_low_balance_skip_no_intratick_flutter(lb_runner, caplog, affordable_first):
    """One sample, same key: a cheap AFFORDABLE level + a pricier BLOCKED level
    (either order) → exactly 1 ENTER, 0 EXIT (no intra-tick EXIT/ENTER flutter)."""
    lb_runner._available_balance = Decimal("50")  # cheap (est 10) ok, pricey (est 100) blocked
    with caplog.at_level("DEBUG"):
        if affordable_first:
            assert lb_runner._preflight_blocks_open(_open_short("10.0", "1.0")) is False
            assert lb_runner._preflight_blocks_open(_open_short("100.0", "1.0")) is True
        else:
            assert lb_runner._preflight_blocks_open(_open_short("100.0", "1.0")) is True
            assert lb_runner._preflight_blocks_open(_open_short("10.0", "1.0")) is False
        lb_runner._reconcile_skip_edges()
    assert len(_info_lines(caplog, "LowBalanceSkip ENTER")) == 1
    assert len(_info_lines(caplog, "LowBalanceSkip EXIT")) == 0


def test_low_balance_skip_failopen_does_not_exit(lb_runner, caplog):
    """A stale-WS-style fail-open sample is evidence-neutral: key stays active,
    no EXIT, no ENTER, window counter unchanged (M4)."""
    with caplog.at_level("DEBUG"):
        lb_runner._available_balance = Decimal("5")
        lb_runner._preflight_blocks_open(_open_short("100.0", "1.0"))
        lb_runner._reconcile_skip_edges()  # ENTER, active
        lb_runner._available_balance = Decimal("0")  # no data → fail-open (None)
        window_before = dict(lb_runner._skip_window)
        assert lb_runner._preflight_blocks_open(_open_short("100.0", "1.0")) is False
        lb_runner._reconcile_skip_edges()
    assert len(_info_lines(caplog, "LowBalanceSkip ENTER")) == 1
    assert len(_info_lines(caplog, "LowBalanceSkip EXIT")) == 0
    assert lb_runner._skip_state[("short", "Sell")]["active"] is True
    assert lb_runner._skip_window == window_before  # fail-open did not count


def test_low_balance_skip_no_false_exit_on_interleaved_event(lb_runner, caplog):
    """Integration shape (M2/High): on_ticker(blocked) → on_order_update(no opens)
    → on_ticker(blocked). Reconcile runs at the top of the NEXT dispatch, so the
    interleaved order-update must NOT manufacture a spurious EXIT: exactly 1
    ENTER, 0 EXIT across the whole sequence."""
    from unittest.mock import Mock as _Mock
    lb_runner._engine = _Mock()
    lb_runner._resolve_qty = lambda intent: intent  # bypass qty calc; keep qty=1
    lb_runner._on_unknown_order = None
    ou = _Mock()
    ou.status = "New"
    ou.order_id = "oid"
    ou.order_link_id = "olid"
    with caplog.at_level("DEBUG"):
        lb_runner._engine.on_event = _Mock(return_value=[_open_short("100.0", "1.0")])
        lb_runner.on_ticker(_Mock())          # blocked grid → scratch written
        lb_runner._engine.on_event = _Mock(return_value=[])
        lb_runner.on_order_update(ou)         # reconcile prior sample → ENTER; no opens
        lb_runner._engine.on_event = _Mock(return_value=[_open_short("100.0", "1.0")])
        lb_runner.on_ticker(_Mock())          # reconcile empty → no EXIT; blocked again
    assert len(_info_lines(caplog, "LowBalanceSkip ENTER")) == 1
    assert len(_info_lines(caplog, "LowBalanceSkip EXIT")) == 0


def test_low_balance_skip_partial_failopen_preserves_sibling_skip(
    strategy_config, mock_executor, instrument_info, caplog
):
    """One sample: long.Buy evaluates fresh-unaffordable (genuine skip) while
    short.Sell fails open (stale peek) → long.Buy ENTERs (its skip is NOT
    discarded), short.Sell untouched/absent from state (Medium-3). Uses a
    stateful provider so the two intents get different freshness."""
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        if calls["n"] == 1:
            return (WalletSnapshot(available_balance=5.0), 1.0)    # fresh → blocked
        return (WalletSnapshot(available_balance=5.0), 100.0)      # stale → fail-open

    r = StrategyRunner(
        strategy_config=strategy_config, executor=mock_executor,
        instrument_info=instrument_info, rest_client=None, clock=lambda: 0.0,
        wallet_provider=provider, wallet_ws_max_age_seconds=45.0,
    )
    with caplog.at_level("DEBUG"):
        assert r._preflight_blocks_open(_open_long("100.0", "1.0")) is True    # fresh, blocked
        assert r._preflight_blocks_open(_open_short("100.0", "1.0")) is False  # stale, fail-open
        r._reconcile_skip_edges()
    enter = _info_lines(caplog, "LowBalanceSkip ENTER")
    assert len(enter) == 1
    assert "direction=long side=Buy" in enter[0].getMessage()
    assert r._skip_state[("long", "Buy")]["active"] is True
    assert ("short", "Sell") not in r._skip_state  # sibling untouched (absent from scratch)


def test_low_balance_skip_per_key_independent(lb_runner, caplog):
    """long.Buy + short.Sell both blocked (2 ENTER); next sample short.Sell
    recovers while long.Buy stays blocked → independent EXIT for short.Sell only."""
    with caplog.at_level("DEBUG"):
        lb_runner._available_balance = Decimal("5")
        lb_runner._preflight_blocks_open(_open_long("100.0", "1.0"))
        lb_runner._preflight_blocks_open(_open_short("100.0", "1.0"))
        lb_runner._reconcile_skip_edges()  # 2 ENTER
        lb_runner._preflight_blocks_open(_open_long("100.0", "1.0"))   # still blocked
        lb_runner._preflight_blocks_open(_open_short("1.0", "1.0"))    # est 1 → affordable
        lb_runner._reconcile_skip_edges()
    assert len(_info_lines(caplog, "LowBalanceSkip ENTER")) == 2
    exits = _info_lines(caplog, "LowBalanceSkip EXIT")
    assert len(exits) == 1
    assert "direction=short side=Sell" in exits[0].getMessage()
    assert lb_runner._skip_state[("long", "Buy")]["active"] is True
    assert lb_runner._skip_state[("short", "Sell")]["active"] is False


# ---- Phase 1 — idle-timeout sweep (stuck-active removed keys) ----

def test_low_balance_skip_idle_timeout_exits_removed_key(lb_runner, lb_clock, caplog):
    """A key removed from the intent set mid-storm (no recovery EXIT) is swept to
    EXIT after `low_balance_skip_exit_idle_seconds`; a later re-block re-ENTERs
    fresh (count reset), NOT a silent Sustained."""
    with caplog.at_level("DEBUG"):
        lb_runner._preflight_blocks_open(_open_short("100.0", "1.0"))
        lb_runner._reconcile_skip_edges()  # ENTER, last_blocked_clock=0
        lb_runner._reconcile_skip_edges()  # t=0, empty scratch, no timeout
        lb_clock.t = 30.0
        lb_runner._reconcile_skip_edges()  # 30 < 60, no timeout
        lb_clock.t = 61.0
        lb_runner._reconcile_skip_edges()  # 61 >= 60 → idle EXIT
    assert len(_info_lines(caplog, "LowBalanceSkip ENTER")) == 1
    exits = _info_lines(caplog, "LowBalanceSkip EXIT")
    assert len(exits) == 1
    assert "idle 60s" in exits[0].getMessage()
    assert lb_runner._skip_state[("short", "Sell")]["active"] is False
    caplog.clear()
    with caplog.at_level("DEBUG"):
        lb_runner._preflight_blocks_open(_open_short("100.0", "1.0"))
        lb_runner._reconcile_skip_edges()
    assert len(_info_lines(caplog, "LowBalanceSkip ENTER")) == 1  # fresh episode


def test_low_balance_skip_idle_timeout_disabled_keeps_active(
    strategy_config, mock_executor, instrument_info, lb_clock, caplog
):
    """`low_balance_skip_exit_idle_seconds=0` disables the sweep → a removed key
    stays active forever (documents the opt-out)."""
    r = _make_runner(strategy_config, mock_executor, instrument_info, lb_clock,
                     low_balance_skip_exit_idle_seconds=0)
    with caplog.at_level("DEBUG"):
        r._preflight_blocks_open(_open_short("100.0", "1.0"))
        r._reconcile_skip_edges()  # ENTER
        lb_clock.t = 100000.0
        r._reconcile_skip_edges()  # no sweep (disabled)
    assert len(_info_lines(caplog, "LowBalanceSkip EXIT")) == 0
    assert r._skip_state[("short", "Sell")]["active"] is True


def test_low_balance_skip_flag_flip_true_false_true_no_stale_replay(lb_runner, caplog):
    """True (scratch written) → flip False (reconcile clears scratch unconditionally,
    no edge) → flip True (reconcile, no new intents) → 0 ENTER, 0 EXIT (no stale
    evidence replayed from the pre-disable sample)."""
    with caplog.at_level("DEBUG"):
        lb_runner._preflight_blocks_open(_open_short("100.0", "1.0"))
        assert ("short", "Sell") in lb_runner._skip_tick_seen  # scratch written while on
        lb_runner._config = lb_runner._config.model_copy(
            update={"low_balance_skip_transition_logs_enabled": False})
        lb_runner._reconcile_skip_edges()  # unconditional clear; flag off → no edge
        assert lb_runner._skip_tick_seen == {}
        lb_runner._config = lb_runner._config.model_copy(
            update={"low_balance_skip_transition_logs_enabled": True})
        lb_runner._reconcile_skip_edges()  # no new intents → nothing replays
    assert _info_lines(caplog, "LowBalanceSkip ENTER") == []
    assert _info_lines(caplog, "LowBalanceSkip EXIT") == []
    assert ("short", "Sell") not in lb_runner._skip_state


def test_low_balance_skip_no_scratch_leak_when_transition_off(
    strategy_config, mock_executor, instrument_info, caplog
):
    """transition off → scratch never written (no unbounded count accrual), but
    the window counter still accumulates. Flip on → first ENTER count == this
    sample's skips only (no inflation carried from the off period) (F2)."""
    r = _make_runner(strategy_config, mock_executor, instrument_info, lambda: 0.0,
                     low_balance_skip_transition_logs_enabled=False)
    with caplog.at_level("DEBUG"):
        for _ in range(3):
            r._preflight_blocks_open(_open_short("100.0", "1.0"))
            assert r._skip_tick_seen == {}  # never written while off
    assert r._skip_window[("short", "Sell")] == 3  # window still accumulates
    r._config = r._config.model_copy(
        update={"low_balance_skip_transition_logs_enabled": True})
    caplog.clear()
    with caplog.at_level("DEBUG"):
        r._preflight_blocks_open(_open_short("100.0", "1.0"))
        r._reconcile_skip_edges()
    assert len(_info_lines(caplog, "LowBalanceSkip ENTER")) == 1
    assert r._skip_state[("short", "Sell")]["count"] == 1  # not inflated by off period


# ---- Phase 2 — periodic 60s summary ----

def test_low_balance_skip_emit_summary_at_60s(lb_runner, lb_clock, caplog):
    """Accumulate skips, advance the clock past the interval, flush → exactly 1
    INFO summary carrying total / per-(direction,side) counts / avail band;
    window reset; a second flush within the window emits nothing."""
    with caplog.at_level("INFO"):
        for _ in range(3):
            lb_runner._preflight_blocks_open(_open_short("100.0", "1.0"))
            lb_runner._reconcile_skip_edges()
        lb_clock.t = 61.0
        lb_runner._emit_skip_summary()
    summ = _info_lines(caplog, "window:")
    assert len(summ) == 1
    msg = summ[0].getMessage()
    assert "total=3" in msg
    assert "short.Sell=3" in msg
    assert "avail_min=" in msg and "avail_max=" in msg
    assert lb_runner._skip_window == {}
    caplog.clear()
    with caplog.at_level("INFO"):
        lb_runner._emit_skip_summary()  # within window → nothing
    assert _info_lines(caplog, "window:") == []


def test_low_balance_skip_summary_fires_with_transition_logs_off(
    strategy_config, mock_executor, instrument_info, lb_clock, caplog
):
    """M1 row 3: transition off + summary on → per-intent DEBUG still fires AND
    the 60s summary still emits (window counter is increment-independent of the
    transition flag); no ENTER/EXIT."""
    r = _make_runner(strategy_config, mock_executor, instrument_info, lb_clock,
                     low_balance_skip_transition_logs_enabled=False,
                     low_balance_skip_summary_enabled=True)
    with caplog.at_level("DEBUG"):
        r._preflight_blocks_open(_open_short("100.0", "1.0"))
        r._reconcile_skip_edges()
        lb_clock.t = 61.0
        r._emit_skip_summary()
    assert len(_skip_debug_lines(caplog)) == 1
    assert len(_info_lines(caplog, "window:")) == 1
    assert _info_lines(caplog, "LowBalanceSkip ENTER") == []


def test_low_balance_skip_transition_on_summary_off(
    strategy_config, mock_executor, instrument_info, lb_clock, caplog
):
    """M1 row 2: transition ON + summary OFF → ENTER edges fire and the per-intent
    DEBUG is suppressed, but NO 60s summary line even when the clock is past the
    interval (the summary kill-switch is honored independently of the transition
    flag, and short-circuits before advancing _skip_summary_last_emit)."""
    r = _make_runner(strategy_config, mock_executor, instrument_info, lb_clock,
                     low_balance_skip_transition_logs_enabled=True,
                     low_balance_skip_summary_enabled=False)
    with caplog.at_level("DEBUG"):
        r._preflight_blocks_open(_open_short("100.0", "1.0"))
        r._reconcile_skip_edges()       # ENTER (transition on)
        lb_clock.t = 61.0
        r._emit_skip_summary()          # summary off → nothing, even past interval
    assert len(_info_lines(caplog, "LowBalanceSkip ENTER")) == 1
    assert _skip_debug_lines(caplog) == []        # per-intent DEBUG suppressed
    assert _info_lines(caplog, "window:") == []   # summary suppressed
    assert r._skip_summary_last_emit == 0.0       # guard returns before clock update


def test_low_balance_skip_summary_empty_window_no_emit(lb_runner, lb_clock, caplog):
    """Clock past interval but zero skips → no summary line (and last-emit advances)."""
    lb_clock.t = 61.0
    with caplog.at_level("INFO"):
        lb_runner._emit_skip_summary()
    assert _info_lines(caplog, "window:") == []
    assert lb_runner._skip_summary_last_emit == 61.0


def test_low_balance_skip_summary_emits_via_drain_hook(lb_runner, lb_clock, caplog):
    """Summary flushes through _drain_pending_chase_intents (production hook), and a
    high monotonic baseline does not cause an immediate summary on the first skip."""
    lb_clock.t = 1000.0
    with caplog.at_level("INFO"):
        lb_runner._drain_pending_chase_intents()  # empty window → advance baseline
        lb_runner._preflight_blocks_open(_open_short("100.0", "1.0"))
        lb_runner._drain_pending_chase_intents()  # skips present but within interval
        assert _info_lines(caplog, "window:") == []
        lb_clock.t = 1061.0
        lb_runner._drain_pending_chase_intents()  # past interval → summary via drain
    summ = _info_lines(caplog, "window:")
    assert len(summ) == 1
    assert "total=1" in summ[0].getMessage()


def test_low_balance_skip_summary_baseline_is_construction_clock(
    strategy_config, mock_executor, instrument_info, caplog
):
    """Regression (review P2): _skip_summary_last_emit baselines to the clock AT
    CONSTRUCTION, not 0.0. Production self._clock is time.monotonic (a large
    value); a 0.0 baseline is only saved from an immediate first-skip flush by the
    first drain's empty-window advance — fragile. This locks the explicit baseline:
    construct at a high clock, record a skip, then emit DIRECTLY (no intervening
    empty-window drain) → no flush until a full interval has actually elapsed."""
    clk = _FakeClock(1000.0)
    r = _make_runner(strategy_config, mock_executor, instrument_info, clk)
    assert r._skip_summary_last_emit == 1000.0  # baselined to construction, not 0.0
    with caplog.at_level("INFO"):
        r._preflight_blocks_open(_open_short("100.0", "1.0"))  # window has 1 skip
        r._emit_skip_summary()                                  # 1000-1000=0 < 60 → no flush
        assert _info_lines(caplog, "window:") == []
        clk.t = 1061.0
        r._emit_skip_summary()                                  # full interval → flush
    assert len(_info_lines(caplog, "window:")) == 1


# ---- Acceptance criterion — both kill-switches off restore current behavior ----

def test_low_balance_skip_killswitches_off_preserve_debug(
    strategy_config, mock_executor, instrument_info, caplog
):
    """Both flags False → byte-for-byte current behavior: per-intent DEBUG on
    every rejection, NO INFO ENTER/EXIT/summary lines."""
    r = _make_runner(strategy_config, mock_executor, instrument_info, lambda: 0.0,
                     low_balance_skip_transition_logs_enabled=False,
                     low_balance_skip_summary_enabled=False)
    with caplog.at_level("DEBUG"):
        assert r._preflight_blocks_open(_open_short("100.0", "1.0")) is True
        r._reconcile_skip_edges()
        r._emit_skip_summary()
    debug = _skip_debug_lines(caplog)
    assert len(debug) == 1
    assert "qty=" in debug[0].getMessage() and "est_cost=" in debug[0].getMessage()
    assert _info_lines(caplog, "LowBalanceSkip") == []
