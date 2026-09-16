"""Integration: health-status file state transitions (feature 0082 / issue #185).

Drives the real auth-cooldown and circuit-breaker trip conditions through the
orchestrator and asserts the JSON status file reflects the transitions, including
recovery and live<->shadow parity.
"""

import json
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from gridcore import EventType, InstrumentInfo, TickerEvent
from gridcore.intents import PlaceLimitIntent
from gridbot.config import (
    AccountConfig,
    GridbotConfig,
    SafetyCapsConfig,
    StrategyConfig,
)
from gridbot.orchestrator import Orchestrator
from gridbot.reconciler import ReconciliationResult


@pytest.fixture(autouse=True)
def _default_instrument_info():
    """Feature 0090: supply a valid exchange tick (0.1, matching _config) so the
    fail-closed instrument fetch does not abort these health-status tests."""
    info = InstrumentInfo(
        symbol="BTCUSDT",
        qty_step=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000"),
    )
    with patch.object(Orchestrator, "_fetch_instrument_info", return_value=info):
        yield


def _config(tmp_path, shadow=False):
    return GridbotConfig(
        accounts=[AccountConfig(name="acc", api_key="k", api_secret="s", testnet=True)],
        strategies=[StrategyConfig(
            strat_id="btcusdt_test", account="acc", symbol="BTCUSDT",
            tick_size=Decimal("0.1"), grid_count=20, grid_step=0.2,
            shadow_mode=shadow,
            safety_caps=SafetyCapsConfig(enabled=True, session_loss_limit=Decimal("10")),
        )],
        database_url="sqlite:///:memory:",
        position_check_interval=60.0,
        status_file_path=str(tmp_path / "status.json"),
        status_file_enabled=True,
    )


def _read(path):
    with open(path) as f:
        return json.load(f)


def _ticker(*, local_ts: datetime) -> TickerEvent:
    """Build a ticker cache entry without involving the normalizer or network."""
    return TickerEvent(
        event_type=EventType.TICKER,
        symbol="BTCUSDT",
        exchange_ts=local_ts,
        local_ts=local_ts,
        last_price=Decimal("50000"),
        mark_price=Decimal("50000"),
        bid1_price=Decimal("49999"),
        ask1_price=Decimal("50001"),
        funding_rate=Decimal("0"),
    )


def _place_intent() -> PlaceLimitIntent:
    """Build a resolved place intent for the freshness-alert re-arm test."""
    return PlaceLimitIntent.create(
        symbol="BTCUSDT",
        side="Buy",
        price=Decimal("49000"),
        qty=Decimal("0.001"),
        grid_level=1,
        direction="long",
    )


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_health_status_auth_cooldown_then_recovery_then_circuit(
    _mock_priv, _mock_pub, _mock_rest, tmp_path,
):
    cfg = _config(tmp_path)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    path = cfg.status_file_path
    executor = orch._strategy_executors["btcusdt_test"]

    # --- auth_cooldown trip (drive max_auth_failures consecutive auth errors) ---
    for _ in range(5):
        executor._handle_error("Bybit API error: [10003] auth failure")
    assert executor.auth_cooldown is True
    orch._health_check_once()
    snap = _read(path)
    assert snap["state"] == "auth_cooldown"
    assert snap["strategies"][0]["state"] == "auth_cooldown"
    assert snap["strategies"][0]["strat_id"] == "btcusdt_test"
    assert snap["gauges"]["auth_cooldown_active"] == 1
    assert snap["gauges"]["auth_cooldown_cycles"] >= 1

    # --- recovery: expire the cooldown -> back to healthy ---
    orch._auth_cooldown.sweep_expired(datetime.now(UTC) + timedelta(days=1))
    assert executor.auth_cooldown is False
    orch._health_check_once()
    assert _read(path)["state"] == "healthy"

    # --- circuit_open: trip C3 via the orchestrator's _safety_caps map ---
    caps = orch._safety_caps["btcusdt_test"]
    assert caps.check_loss_breaker(
        session_realized_pnl=Decimal("-50"), now_utc=datetime.now(UTC)
    ) is True
    orch._health_check_once()
    snap2 = _read(path)
    assert snap2["state"] == "circuit_open"
    assert snap2["gauges"]["loss_breaker_latched"] == 1


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_health_status_shadow_parity(_mock_priv, _mock_pub, _mock_rest, tmp_path):
    cfg = _config(tmp_path, shadow=True)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    path = cfg.status_file_path

    # A shadow strat still flips to circuit_open (C3 runs independent of submit).
    orch._safety_caps["btcusdt_test"].check_loss_breaker(
        session_realized_pnl=Decimal("-50"), now_utc=datetime.now(UTC)
    )
    orch._health_check_once()
    snap = _read(path)
    assert snap["state"] == "circuit_open"
    assert snap["strategies"][0]["shadow"] is True
    # Identical snapshot shape in shadow vs live (parity).
    assert set(snap.keys()) == {"state", "generated_at", "strategies", "metrics", "gauges"}
    assert snap["metrics"]["orders_placed"] == 0  # no real submit in shadow


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_health_status_includes_reconcile_truncations(
    _mock_priv, _mock_pub, _mock_rest, tmp_path,
):
    cfg = _config(tmp_path)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    orch._build_routing_maps()
    reconciler = orch._reconcilers["acc"]
    reconciler.reconcile_reconnect = Mock(
        return_value=ReconciliationResult(orders_fetched=1, truncated=True)
    )

    orch._order_sync_once()
    orch._health_check_once()

    assert _read(cfg.status_file_path)["metrics"]["reconcile_truncations"]["btcusdt_test"] == 1


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_starting_snapshot_written_on_start(_mock_priv, _mock_pub, _mock_rest, tmp_path):
    cfg = _config(tmp_path)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    # start() sets _start_time and writes the `starting` snapshot before run().
    orch.start()
    snap = _read(cfg.status_file_path)
    assert snap["state"] == "starting"
    assert snap["gauges"]["uptime_seconds"] >= 0


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_health_status_degraded_delta_then_recovery(
    _mock_priv, _mock_pub, _mock_rest, tmp_path,
):
    cfg = _config(tmp_path)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    path = cfg.status_file_path
    runner = orch._runners["btcusdt_test"]

    # Baseline sweep: no failures -> healthy (seeds the last-count for the delta).
    orch._health_check_once()
    assert _read(path)["state"] == "healthy"

    # Simulate a NEW dirty-REST failure since the last sweep (delta > 0) -> degraded.
    runner._dirty_rest_refresh_failure_count += 1
    orch._health_check_once()
    assert _read(path)["state"] == "degraded"

    # No NEW failure on the next sweep -> delta clears -> recovers to healthy.
    # Guards against the sticky monotonic-absolute bug (review #195 P1).
    orch._health_check_once()
    assert _read(path)["state"] == "healthy"


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_health_status_degraded_requires_new_failure_not_sticky(
    _mock_priv, _mock_pub, _mock_rest, tmp_path,
):
    """Degraded keys off a strict delta (cur > prev), not the absolute count.

    A strat that ALREADY had dirty-REST failures before the first sweep must NOT
    be stuck degraded: the first sweep seeds prev=cur (so cur > prev is False),
    and a later sweep at the SAME count stays healthy. Proves `>` not `>=`.
    """
    cfg = _config(tmp_path)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    path = cfg.status_file_path
    runner = orch._runners["btcusdt_test"]

    # Pre-existing (historical) failures already on the monotonic counter.
    runner._dirty_rest_refresh_failure_count = 3
    orch._health_check_once()
    assert _read(path)["state"] == "healthy"  # seeded prev=cur -> no delta

    # Same count on the next sweep -> still healthy (strict >, not >=).
    orch._health_check_once()
    assert _read(path)["state"] == "healthy"


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_snapshot_reports_ticker_age_and_status(
    _mock_priv, _mock_pub, _mock_rest, tmp_path,
):
    """Feature 0106 — fresh cache data is surfaced as a current gauge."""
    cfg = _config(tmp_path)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    orch._latest_ticker["BTCUSDT"] = _ticker(local_ts=datetime.now(UTC))

    orch._health_check_once()

    strategy = _read(cfg.status_file_path)["strategies"][0]
    assert strategy["ticker_status"] == "fresh"
    assert strategy["ticker_age_seconds"] >= 0
    assert strategy["max_ticker_age_seconds"] == 15.0


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_stale_ticker_degrades_snapshot(_mock_priv, _mock_pub, _mock_rest, tmp_path):
    """A stale ticker is the lowest-priority state that degrades health."""
    cfg = _config(tmp_path)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    orch._latest_ticker["BTCUSDT"] = _ticker(
        local_ts=datetime.now(UTC) - timedelta(seconds=16)
    )

    orch._health_check_once()

    snapshot = _read(cfg.status_file_path)
    assert snapshot["state"] == "degraded"
    assert snapshot["strategies"][0]["ticker_status"] == "stale"


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_no_data_ticker_does_not_degrade(_mock_priv, _mock_pub, _mock_rest, tmp_path):
    """Startup before a first ticker remains healthy rather than false-degraded."""
    cfg = _config(tmp_path)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])

    orch._health_check_once()

    snapshot = _read(cfg.status_file_path)
    assert snapshot["state"] == "healthy"
    assert snapshot["strategies"][0]["ticker_status"] == "no_data"
    assert snapshot["strategies"][0]["ticker_age_seconds"] is None


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_health_sweep_rearms_alert_latch(_mock_priv, _mock_pub, _mock_rest, tmp_path):
    """A fresh periodic assessment re-arms alerts without placement activity."""
    cfg = _config(tmp_path)
    notifier = Mock()
    orch = Orchestrator(cfg, notifier=notifier)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    runner = orch._runners["btcusdt_test"]
    runner._ticker_freshness_alerted = True
    orch._latest_ticker["BTCUSDT"] = _ticker(local_ts=datetime.now(UTC))

    # The snapshot writer is deliberately broken for this sweep. The plan puts
    # the assessment in _health_check_once and NOT inside _write_health_snapshot,
    # whose try/except swallows everything (orchestrator.py:1442-1446) — if the
    # assessment ever moved in there, a failing write would silently skip the
    # re-arm. Without this, the test passes in both placements.
    orch._health_writer.write = Mock(side_effect=RuntimeError("status write down"))

    orch._health_check_once()
    assert runner._ticker_freshness_alerted is False

    orch._latest_ticker["BTCUSDT"] = _ticker(
        local_ts=datetime.now(UTC) - timedelta(seconds=16)
    )
    runner._execute_place_intent(_place_intent(), {"long": [], "short": []})

    notifier.alert.assert_called_once()


@patch("gridbot.orchestrator.BybitRestClient")
@patch("gridbot.orchestrator.PublicWebSocketClient")
@patch("gridbot.orchestrator.PrivateWebSocketClient")
def test_worse_existing_degradation_not_overwritten(
    _mock_priv, _mock_pub, _mock_rest, tmp_path,
):
    """A stale ticker cannot overwrite a worse circuit-open health state."""
    cfg = _config(tmp_path)
    orch = Orchestrator(cfg)
    orch._init_account(cfg.accounts[0])
    orch._init_strategy(cfg.strategies[0])
    orch._latest_ticker["BTCUSDT"] = _ticker(
        local_ts=datetime.now(UTC) - timedelta(seconds=16)
    )
    orch._safety_caps["btcusdt_test"].check_loss_breaker(
        session_realized_pnl=Decimal("-50"), now_utc=datetime.now(UTC)
    )

    orch._health_check_once()

    strategy = _read(cfg.status_file_path)["strategies"][0]
    assert strategy["state"] == "circuit_open"
    assert strategy["ticker_status"] == "stale"
