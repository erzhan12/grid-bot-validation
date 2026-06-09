"""Feature 0069 — orchestrator-side state-divergence detector (issue #151).

Covers the _force_reconcile_strat refactor (B-1 single-call both-directions +
breaker-WARNING gating), the _trigger_divergence_reconcile wrapper (throttle,
kill-switch, breaker-cooldown suppression), and the four signals (placement-
failure union wiring, retry-budget edge, REST-size delta sweep, post-WS-recovery
enqueue/drain/fast-track).

    uv run pytest apps/gridbot/tests/test_orchestrator_divergence.py
"""

from datetime import datetime, UTC
from decimal import Decimal
from unittest.mock import Mock, MagicMock

import pytest

from gridcore.position import DirectionType

from gridbot.config import GridbotConfig, AccountConfig, StrategyConfig
from gridbot.orchestrator import Orchestrator
from gridbot.reconciler import ReconciliationResult


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

def _strategy_config(**overrides) -> StrategyConfig:
    base = dict(
        strat_id="btcusdt_test",
        account="test_account",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        grid_count=20,
        grid_step=0.2,
    )
    base.update(overrides)
    return StrategyConfig(**base)


def _gridbot_config(strategy_config=None, **grid_overrides) -> GridbotConfig:
    cfg = dict(
        accounts=[AccountConfig(
            name="test_account", api_key="k", api_secret="s", testnet=True,
        )],
        strategies=[strategy_config or _strategy_config()],
        database_url="sqlite:///:memory:",
        position_check_interval=60.0,
    )
    cfg.update(grid_overrides)
    return GridbotConfig(**cfg)


def _wire(gridbot_config, *, instrument_info=None):
    """Orchestrator + one Mock runner + Mock reconciler + routing map."""
    orch = Orchestrator(gridbot_config)
    orch._notifier = Mock()  # avoid real Telegram side effects in alert paths
    runner = Mock()
    runner.strat_id = "btcusdt_test"
    runner.symbol = "BTCUSDT"
    # Ints so the signal-2 block in _health_check_once does not raise on Mock
    # comparisons (which would abort the method before the WS reconnect loop).
    runner.truncate_breaker_reconcile_count = 0
    runner.dirty_rest_refresh_failure_count = 0
    if instrument_info is not None:
        runner._instrument_info = instrument_info
    reconciler = Mock()
    reconciler.reconcile_reconnect = MagicMock(return_value=ReconciliationResult())
    orch._runners["btcusdt_test"] = runner
    orch._reconcilers["test_account"] = reconciler
    orch._account_to_runners["test_account"] = [runner]
    return orch, runner, reconciler


@pytest.fixture
def fixed_clock(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("gridbot.orchestrator.time.monotonic", lambda: clock["now"])
    return clock


# ==========================================================================
# B-1 — _force_reconcile_strat single-call both-directions
# ==========================================================================

def test_force_reconcile_direction_none_refreshes_both(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    ran = orch._force_reconcile_strat("btcusdt_test", None)
    assert ran is True
    reconciler.reconcile_reconnect.assert_called_once_with(runner)
    assert runner._refresh_position_size_from_rest.call_count == 2
    runner._refresh_position_size_from_rest.assert_any_call(DirectionType.LONG, force=True)
    runner._refresh_position_size_from_rest.assert_any_call(DirectionType.SHORT, force=True)


def test_force_reconcile_second_call_within_cooldown_returns_false(fixed_clock):
    """Single-call model: a second back-to-back direction=None call within the
    cooldown returns False and does NO reconcile/refresh (the rate-limit ts is
    set once per call; the second direction is never silently dropped)."""
    orch, runner, reconciler = _wire(_gridbot_config())
    assert orch._force_reconcile_strat("btcusdt_test", None) is True
    reconciler.reconcile_reconnect.reset_mock()
    runner._refresh_position_size_from_rest.reset_mock()
    assert orch._force_reconcile_strat("btcusdt_test", None) is False
    reconciler.reconcile_reconnect.assert_not_called()
    runner._refresh_position_size_from_rest.assert_not_called()


def test_force_reconcile_specific_direction_unchanged(fixed_clock):
    """Breaker-trip caller path: one reconcile + one refresh for the side."""
    orch, runner, reconciler = _wire(_gridbot_config())
    assert orch._force_reconcile_strat("btcusdt_test", "long") is True
    reconciler.reconcile_reconnect.assert_called_once_with(runner)
    runner._refresh_position_size_from_rest.assert_called_once_with("long", force=True)


def test_force_reconcile_missing_runner_returns_false(fixed_clock):
    orch = Orchestrator(_gridbot_config())
    assert orch._force_reconcile_strat("nope", None) is False


# B-1 breaker-WARNING gating ------------------------------------------------

def test_breaker_warning_emitted_by_default(fixed_clock, caplog):
    orch, runner, reconciler = _wire(_gridbot_config())
    with caplog.at_level("WARNING", logger="gridbot.orchestrator"):
        orch._force_reconcile_strat("btcusdt_test", "long")  # default True
    msgs = [r.getMessage() for r in caplog.records]
    assert any("110017 breaker tripped — forcing long" in m for m in msgs)


def test_detector_fire_suppresses_breaker_warning(fixed_clock, caplog):
    orch, runner, reconciler = _wire(_gridbot_config())
    with caplog.at_level("WARNING", logger="gridbot.orchestrator"):
        orch._trigger_divergence_reconcile("btcusdt_test", "rest_failure_mix", 10)
    msgs = [r.getMessage() for r in caplog.records]
    # Exactly ONE detector WARNING, NO breaker line (and no 'None' direction).
    assert sum("state-divergence detected" in m for m in msgs) == 1
    assert not any("110017 breaker tripped — forcing" in m for m in msgs)
    assert not any("None position + order reconcile" in m for m in msgs)


# ==========================================================================
# Wrapper — throttle, kill-switch, breaker-cooldown suppression
# ==========================================================================

def test_wrapper_detector_throttle(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    orch._trigger_divergence_reconcile("btcusdt_test", "rest_failure_mix", 10)
    assert reconciler.reconcile_reconnect.call_count == 1
    # Second within the 300s detector throttle → suppressed.
    orch._trigger_divergence_reconcile("btcusdt_test", "rest_failure_mix", 11)
    assert reconciler.reconcile_reconnect.call_count == 1
    # After the detector throttle (and past the 60s breaker cooldown) → fires.
    fixed_clock["now"] = 1000.0 + 301.0
    orch._trigger_divergence_reconcile("btcusdt_test", "rest_failure_mix", 12)
    assert reconciler.reconcile_reconnect.call_count == 2


def test_wrapper_kill_switch(fixed_clock):
    orch, runner, reconciler = _wire(
        _gridbot_config(_strategy_config(divergence_detector_enabled=False))
    )
    orch._trigger_divergence_reconcile("btcusdt_test", "rest_failure_mix", 10)
    reconciler.reconcile_reconnect.assert_not_called()


def test_wrapper_suppressed_by_breaker_cooldown_no_misleading_warning(
    fixed_clock, caplog
):
    """F-1-6: a forced reconcile within the breaker's 60s cooldown returns False;
    the wrapper must NOT emit the WARNING, NOT clear the dedup cache, NOT bump the
    detector throttle. After the cooldown elapses the same signal fires fully."""
    orch, runner, reconciler = _wire(_gridbot_config())
    # Simulate a breaker trip that just set the internal cooldown.
    orch._force_reconcile_last_at["btcusdt_test"] = 1000.0

    with caplog.at_level("WARNING", logger="gridbot.orchestrator"):
        orch._trigger_divergence_reconcile("btcusdt_test", "rest_failure_mix", 10)
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("state-divergence detected" in m for m in msgs)
    reconciler.reconcile_reconnect.assert_not_called()
    runner.clear_dedup_cache.assert_not_called()
    assert "btcusdt_test" not in orch._divergence_last_fire_at

    # After the 60s breaker cooldown → fires fully.
    fixed_clock["now"] = 1000.0 + 61.0
    with caplog.at_level("WARNING", logger="gridbot.orchestrator"):
        orch._trigger_divergence_reconcile("btcusdt_test", "rest_failure_mix", 10)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("state-divergence detected" in m for m in msgs)
    runner.clear_dedup_cache.assert_called_once()
    assert orch._divergence_last_fire_at["btcusdt_test"] == 1061.0


# ==========================================================================
# Signal 2 — retry-budget exhaustion edge
# ==========================================================================

def _wire_for_signal2(budget=5):
    orch, runner, reconciler = _wire(
        _gridbot_config(_strategy_config(divergence_retry_budget=budget))
    )
    runner.dirty_rest_refresh_failure_count = 0
    orch._trigger_divergence_reconcile = Mock()  # isolate edge logic from throttle
    return orch, runner


def test_signal2_fires_on_edge_then_not_while_parked_then_on_next_edge():
    orch, runner = _wire_for_signal2(budget=5)
    runner.truncate_breaker_reconcile_count = 5
    orch._health_check_once()  # edge: 5 >= 5, never fired
    assert orch._trigger_divergence_reconcile.call_count == 1
    orch._trigger_divergence_reconcile.assert_called_with("btcusdt_test", "retry_budget", 5)
    orch._health_check_once()  # parked at 5 → no re-fire
    assert orch._trigger_divergence_reconcile.call_count == 1
    runner.truncate_breaker_reconcile_count = 6  # advances to a new edge
    orch._health_check_once()
    assert orch._trigger_divergence_reconcile.call_count == 2
    orch._trigger_divergence_reconcile.assert_called_with("btcusdt_test", "retry_budget", 6)


def test_signal2_below_budget_does_not_fire():
    orch, runner = _wire_for_signal2(budget=5)
    runner.truncate_breaker_reconcile_count = 4
    orch._health_check_once()
    orch._trigger_divergence_reconcile.assert_not_called()


def test_signal2_disabled_does_not_fire():
    orch, runner, reconciler = _wire(
        _gridbot_config(_strategy_config(
            divergence_detector_enabled=False, divergence_retry_budget=5,
        ))
    )
    runner.dirty_rest_refresh_failure_count = 0
    runner.truncate_breaker_reconcile_count = 9
    orch._trigger_divergence_reconcile = Mock()
    orch._health_check_once()
    orch._trigger_divergence_reconcile.assert_not_called()


def test_signal2_retries_after_breaker_cooldown_suppresses_reconcile(fixed_clock):
    """Budget edge must not be consumed when reconcile is rate-limited.

    Otherwise a parked truncate_breaker_reconcile_count (no new 110017s while
    placements are blocked) never gets the both-direction backstop reconcile.
    """
    orch, runner, reconciler = _wire(
        _gridbot_config(_strategy_config(
            divergence_retry_budget=5,
            truncate_breaker_cooldown_seconds=60.0,
        ))
    )
    runner.dirty_rest_refresh_failure_count = 0
    runner.truncate_breaker_reconcile_count = 5
    orch._force_reconcile_last_at["btcusdt_test"] = 1000.0
    fixed_clock["now"] = 1030.0  # 30s later — still inside 60s cooldown

    orch._health_check_once()
    assert "btcusdt_test" not in orch._divergence_budget_last_fired
    reconciler.reconcile_reconnect.assert_not_called()

    fixed_clock["now"] = 1070.0  # cooldown expired — same edge should retry
    orch._health_check_once()
    assert orch._divergence_budget_last_fired["btcusdt_test"] == 5
    reconciler.reconcile_reconnect.assert_called_once_with(runner)


# ==========================================================================
# Signal 3 — REST-vs-local position-size delta sweep
# ==========================================================================

def _wire_for_signal3(*, long_rest, short_rest, long_local, short_local,
                      qty_step=Decimal("0.1"), multiplier=5.0,
                      instrument_info="real", enabled=True):
    cfg = _strategy_config(
        divergence_detector_enabled=enabled,
        divergence_size_delta_qty_step_multiplier=multiplier,
    )
    orch, runner, reconciler = _wire(_gridbot_config(cfg))
    if instrument_info == "real":
        runner._instrument_info = Mock(qty_step=qty_step)
    else:
        runner._instrument_info = instrument_info
    runner._long_position = Mock(size=long_local)
    runner._short_position = Mock(size=short_local)

    def _rest(direction):
        return long_rest if direction == DirectionType.LONG else short_rest
    runner.rest_position_size = MagicMock(side_effect=_rest)
    return orch, runner, reconciler


def test_signal3_only_long_diverges_fires_once_direction_none(fixed_clock):
    # qty_step 0.1 * 5 = 0.5 threshold. LONG Δ=1.0 (>0.5), SHORT Δ=0.0.
    orch, runner, reconciler = _wire_for_signal3(
        long_rest=Decimal("1.0"), short_rest=Decimal("0.0"),
        long_local=Decimal("0.0"), short_local=Decimal("0.0"),
    )
    orch._trigger_divergence_reconcile = Mock()
    orch._divergence_size_check_once()
    assert orch._trigger_divergence_reconcile.call_count == 1
    args, kwargs = orch._trigger_divergence_reconcile.call_args
    assert args[0] == "btcusdt_test"
    assert args[1] == "rest_size_delta"
    assert "long" in args[2]  # evidence names the diverging side
    assert kwargs.get("direction") is None


def test_signal3_neither_diverges_no_fire_and_no_mutation(fixed_clock):
    orch, runner, reconciler = _wire_for_signal3(
        long_rest=Decimal("0.2"), short_rest=Decimal("0.2"),
        long_local=Decimal("0.2"), short_local=Decimal("0.2"),
    )
    orch._trigger_divergence_reconcile = Mock()
    orch._divergence_size_check_once()
    orch._trigger_divergence_reconcile.assert_not_called()
    # The read-only sweep must not mutate the mirror.
    assert runner._long_position.size == Decimal("0.2")
    assert runner._short_position.size == Decimal("0.2")


def test_signal3_both_diverge_single_full_reconcile_throttle_bumped_once(fixed_clock):
    """P1-a: both sides diverge in one sweep → EXACTLY ONE _force_reconcile_strat
    (direction=None), both mirrors refreshed, throttle bumped once — no second
    per-side call that would be throttle-suppressed."""
    orch, runner, reconciler = _wire_for_signal3(
        long_rest=Decimal("1.0"), short_rest=Decimal("2.0"),
        long_local=Decimal("0.0"), short_local=Decimal("0.0"),
    )
    orch._divergence_size_check_once()  # real wrapper
    reconciler.reconcile_reconnect.assert_called_once_with(runner)
    assert runner._refresh_position_size_from_rest.call_count == 2
    assert list(orch._divergence_last_fire_at.keys()) == ["btcusdt_test"]


def test_signal3_skips_when_instrument_info_none(fixed_clock):
    orch, runner, reconciler = _wire_for_signal3(
        long_rest=Decimal("1.0"), short_rest=Decimal("0.0"),
        long_local=Decimal("0.0"), short_local=Decimal("0.0"),
        instrument_info=None,
    )
    orch._trigger_divergence_reconcile = Mock()
    orch._divergence_size_check_once()
    orch._trigger_divergence_reconcile.assert_not_called()


def test_signal3_skips_when_qty_step_not_positive(fixed_clock):
    orch, runner, reconciler = _wire_for_signal3(
        long_rest=Decimal("1.0"), short_rest=Decimal("0.0"),
        long_local=Decimal("0.0"), short_local=Decimal("0.0"),
        qty_step=Decimal("0"),
    )
    orch._trigger_divergence_reconcile = Mock()
    orch._divergence_size_check_once()
    orch._trigger_divergence_reconcile.assert_not_called()


def test_signal3_none_rest_read_skips_direction(fixed_clock):
    # LONG REST read fails (None) → skipped; SHORT in-sync → no fire.
    orch, runner, reconciler = _wire_for_signal3(
        long_rest=None, short_rest=Decimal("0.0"),
        long_local=Decimal("9.0"), short_local=Decimal("0.0"),
    )
    orch._trigger_divergence_reconcile = Mock()
    orch._divergence_size_check_once()
    orch._trigger_divergence_reconcile.assert_not_called()


def test_signal3_disabled_does_not_read_rest(fixed_clock):
    orch, runner, reconciler = _wire_for_signal3(
        long_rest=Decimal("1.0"), short_rest=Decimal("0.0"),
        long_local=Decimal("0.0"), short_local=Decimal("0.0"),
        enabled=False,
    )
    orch._divergence_size_check_once()
    runner.rest_position_size.assert_not_called()


# ==========================================================================
# Signal 4 — post-WS-recovery enqueue / drain / fast-track
# ==========================================================================

def test_signal4_private_disconnect_enqueues_account_strats(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    orch._private_ws["test_account"] = Mock()
    orch._on_ws_disconnect("test_account", "private", datetime.now(UTC))
    assert orch._pending_post_recovery_reconcile == {"btcusdt_test"}


def test_signal4_public_disconnect_does_not_enqueue(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    orch._public_ws["test_account"] = Mock()
    orch._on_ws_disconnect("test_account", "public", datetime.now(UTC))
    assert orch._pending_post_recovery_reconcile == set()


def test_signal4_fanout_and_dedup(fixed_clock):
    cfg2 = _strategy_config(strat_id="ethusdt_test", symbol="ETHUSDT")
    gc = _gridbot_config(_strategy_config())
    gc.strategies.append(cfg2)
    orch = Orchestrator(gc)
    r1, r2 = Mock(), Mock()
    r1.strat_id, r2.strat_id = "btcusdt_test", "ethusdt_test"
    orch._account_to_runners["test_account"] = [r1, r2]
    orch._enqueue_post_recovery_reconcile("test_account")
    orch._enqueue_post_recovery_reconcile("test_account")  # dedup
    assert orch._pending_post_recovery_reconcile == {"btcusdt_test", "ethusdt_test"}


def test_signal4_enqueue_skipped_when_disabled(fixed_clock):
    orch, runner, reconciler = _wire(
        _gridbot_config(_strategy_config(divergence_detector_enabled=False))
    )
    orch._enqueue_post_recovery_reconcile("test_account")
    assert orch._pending_post_recovery_reconcile == set()


def _make_tick_safe(orch, clock_now):
    """Neutralise the other periodic checks so _tick exercises only the signal-4
    drain + the order-sync gate."""
    far = clock_now + 1e9
    orch._next_position_check = far
    orch._next_health_check = far
    orch._next_ws_health_check = far
    orch._next_retry_tick = far
    orch._next_divergence_size_check = far
    orch._position_fetcher = Mock()
    orch._order_sync_once = Mock()


def test_signal4_drain_fires_once_per_strat_and_empties(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    _make_tick_safe(orch, fixed_clock["now"])
    orch._pending_post_recovery_reconcile = {"btcusdt_test"}
    orch._tick()
    reconciler.reconcile_reconnect.assert_called_once_with(runner)
    assert orch._pending_post_recovery_reconcile == set()


def test_signal4_throttle_suppressed_is_dropped(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    _make_tick_safe(orch, fixed_clock["now"])
    # Detector throttle ACTIVE (a recent unrelated fire).
    orch._divergence_last_fire_at["btcusdt_test"] = fixed_clock["now"]
    orch._pending_post_recovery_reconcile = {"btcusdt_test"}
    orch._tick()
    reconciler.reconcile_reconnect.assert_not_called()  # suppressed
    assert orch._pending_post_recovery_reconcile == set()  # dropped, not retried
    # A later tick with nothing pending does not resurrect it.
    fixed_clock["now"] += 10_000.0
    _make_tick_safe(orch, fixed_clock["now"])
    orch._tick()
    reconciler.reconcile_reconnect.assert_not_called()


def test_signal4_dropped_reconcile_fast_tracks_order_sync(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())  # order_sync_interval default 61
    _make_tick_safe(orch, fixed_clock["now"])
    orch._next_order_sync = fixed_clock["now"] + 1e9  # far future
    orch._divergence_last_fire_at["btcusdt_test"] = fixed_clock["now"]  # throttle active
    orch._pending_post_recovery_reconcile = {"btcusdt_test"}
    orch._tick()
    # Fast-track zeroed _next_order_sync and the gate consumed it THIS tick.
    orch._order_sync_once.assert_called_once()
    # Still dropped via the wrapper; not retried.
    reconciler.reconcile_reconnect.assert_not_called()
    assert orch._pending_post_recovery_reconcile == set()
    # The gate reset _next_order_sync (does not stay 0.0).
    assert orch._next_order_sync != 0.0


def test_signal4_no_fast_track_when_order_sync_disabled(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config(order_sync_interval=0.0))
    _make_tick_safe(orch, fixed_clock["now"])
    orch._next_order_sync = fixed_clock["now"] + 1e9
    orch._divergence_last_fire_at["btcusdt_test"] = fixed_clock["now"]  # throttle active
    orch._pending_post_recovery_reconcile = {"btcusdt_test"}
    orch._tick()
    orch._order_sync_once.assert_not_called()  # gate requires interval > 0
    assert orch._pending_post_recovery_reconcile == set()  # still dropped


# Signal 4 — the production trigger-path enqueue wirings -----------------------

def test_signal4_health_check_private_reconnect_enqueues(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    priv = Mock()
    priv.is_connected.return_value = False
    pub = Mock()
    pub.is_connected.return_value = True  # public stays up → skipped
    orch._private_ws["test_account"] = priv
    orch._public_ws["test_account"] = pub
    orch._health_check_once()
    assert orch._pending_post_recovery_reconcile == {"btcusdt_test"}


def test_signal4_health_check_public_reconnect_does_not_enqueue(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    pub = Mock()
    pub.is_connected.return_value = False
    orch._public_ws["test_account"] = pub
    orch._health_check_once()
    assert orch._pending_post_recovery_reconcile == set()


def test_signal4_health_check_enqueues_even_when_reconnect_raises(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    # _health_check_once iterates self._public_ws.keys(), so the account must be
    # present there for the per-account loop to run (production always has both).
    pub = Mock()
    pub.is_connected.return_value = True
    orch._public_ws["test_account"] = pub
    priv = Mock()
    priv.is_connected.return_value = False
    priv.connect.side_effect = Exception("boom")  # reconnect fails
    orch._private_ws["test_account"] = priv
    orch._health_check_once()  # must not raise
    # The dead socket is the divergence event — enqueue regardless of reconnect.
    assert orch._pending_post_recovery_reconcile == {"btcusdt_test"}


def test_signal4_ws_health_check_private_reset_enqueues(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    priv = Mock()
    priv.is_socket_alive.return_value = False
    orch._private_ws["test_account"] = priv
    orch._ws_health_check_once()
    assert orch._pending_post_recovery_reconcile == {"btcusdt_test"}
    priv.reset.assert_called_once()


def test_signal4_ws_health_check_public_reset_does_not_enqueue(fixed_clock):
    orch, runner, reconciler = _wire(_gridbot_config())
    pub = Mock()
    pub.is_socket_alive.return_value = False
    orch._public_ws["test_account"] = pub
    orch._ws_health_check_once()
    assert orch._pending_post_recovery_reconcile == set()


def test_signal4_ws_health_check_enqueues_even_when_reset_raises(fixed_clock):
    """The enqueue happens on DETECTION (before reset), so a failing reset() does
    not skip scheduling the REST-based forced reconcile."""
    orch, runner, reconciler = _wire(_gridbot_config())
    priv = Mock()
    priv.is_socket_alive.return_value = False
    priv.reset.side_effect = Exception("boom")
    orch._private_ws["test_account"] = priv
    orch._ws_health_check_once()  # must not raise
    assert orch._pending_post_recovery_reconcile == {"btcusdt_test"}


def test_signal4_fresh_disconnect_after_throttle_reconciles(fixed_clock):
    """The pending set is per-event, not a deferral queue: a suppressed strat is
    dropped, but a FRESH disconnect after the throttle window reconciles normally."""
    orch, runner, reconciler = _wire(_gridbot_config())
    _make_tick_safe(orch, fixed_clock["now"])
    orch._divergence_last_fire_at["btcusdt_test"] = fixed_clock["now"]  # throttle active
    orch._pending_post_recovery_reconcile = {"btcusdt_test"}
    orch._tick()  # suppressed → dropped
    reconciler.reconcile_reconnect.assert_not_called()
    assert orch._pending_post_recovery_reconcile == set()
    # Advance past the 300s detector throttle; a fresh disconnect re-enqueues.
    fixed_clock["now"] += 301.0
    _make_tick_safe(orch, fixed_clock["now"])
    orch._pending_post_recovery_reconcile = {"btcusdt_test"}
    orch._tick()
    reconciler.reconcile_reconnect.assert_called_once_with(runner)
    assert orch._pending_post_recovery_reconcile == set()
