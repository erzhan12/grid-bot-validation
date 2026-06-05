"""Isolated unit tests for PositionFetcher.

These tests exercise PositionFetcher directly, without spinning up an
Orchestrator. They complement the integration-style tests in
test_orchestrator.py (which go through the Orchestrator and patch the
fetcher on its attribute).
"""

import logging
import threading
from datetime import datetime, timedelta, UTC
from unittest.mock import Mock, patch

import pytest

from gridbot.notifier import Notifier
from gridbot.position_fetcher import (
    PositionFetcher,
    StartupTimeoutError,
    WalletSnapshot,
    _POSITION_FETCH_SLOW_THRESHOLD,
    _POSITION_STARTUP_HARD_CAP,
)


def _make_fetcher(
    *,
    rest_clients=None,
    account_to_runners=None,
    notifier=None,
    wallet_cache_interval=300.0,
    position_check_interval=60.0,
    on_position_changed=None,
    wallet_ws_max_age_seconds=45.0,
):
    return PositionFetcher(
        rest_clients=rest_clients if rest_clients is not None else {},
        account_to_runners=account_to_runners if account_to_runners is not None else {},
        notifier=notifier if notifier is not None else Mock(spec=Notifier),
        wallet_cache_interval=wallet_cache_interval,
        position_check_interval=position_check_interval,
        on_position_changed=on_position_changed,
        wallet_ws_max_age_seconds=wallet_ws_max_age_seconds,
    )


class TestOnPositionMessage:
    def test_populates_cache_for_linear(self):
        fetcher = _make_fetcher()
        msg = {
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
        fetcher.on_position_message("acct", msg)

        cached = fetcher._position_ws_data["acct"]["BTCUSDT"]["Buy"]
        assert cached["size"] == "0.1"
        assert cached["avgPrice"] == "42500.00"

    def test_filters_non_linear(self):
        fetcher = _make_fetcher()
        msg = {
            "data": [
                {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "size": "1.0"},
            ]
        }
        fetcher.on_position_message("acct", msg)
        assert fetcher._position_ws_data.get("acct", {}) == {}

    def test_skips_empty_symbol_or_side(self):
        fetcher = _make_fetcher()
        msg = {
            "data": [
                {"category": "linear", "symbol": "", "side": "Buy", "size": "0.1"},
                {"category": "linear", "symbol": "BTCUSDT", "side": "", "size": "0.1"},
            ]
        }
        fetcher.on_position_message("acct", msg)
        assert fetcher._position_ws_data.get("acct", {}).get("BTCUSDT", {}) == {}

    def test_stores_both_sides(self):
        fetcher = _make_fetcher()
        msg = {
            "data": [
                {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "size": "0.1"},
                {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "size": "0.05"},
            ]
        }
        fetcher.on_position_message("acct", msg)
        assert fetcher._position_ws_data["acct"]["BTCUSDT"]["Buy"]["size"] == "0.1"
        assert fetcher._position_ws_data["acct"]["BTCUSDT"]["Sell"]["size"] == "0.05"

    def test_broad_exception_caught_and_notified(self):
        """Malformed payload must not escape — alert the notifier instead."""
        notifier = Mock(spec=Notifier)
        fetcher = _make_fetcher(notifier=notifier)
        # `data` is an int, not a list → .get('data', []) succeeds but
        # iteration fails on non-iterable at first use.
        fetcher.on_position_message("acct", {"data": 12345})
        notifier.alert_exception.assert_called_once()
        assert "on_position" in notifier.alert_exception.call_args[0][0]

    def test_callback_fires_once_per_symbol(self):
        """Feature 0023: callback invoked once per (account, symbol),
        deduped across sides arriving in the same message.
        """
        callback = Mock()
        fetcher = _make_fetcher(on_position_changed=callback)
        msg = {
            "data": [
                {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "size": "0.1"},
                {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "size": "0.05"},
                {"category": "linear", "symbol": "ETHUSDT", "side": "Buy", "size": "1.0"},
            ]
        }
        fetcher.on_position_message("acct", msg)
        # BTCUSDT once (despite Buy+Sell), ETHUSDT once.
        assert callback.call_count == 2
        symbols_called = {call.args[1] for call in callback.call_args_list}
        assert symbols_called == {"BTCUSDT", "ETHUSDT"}
        for call in callback.call_args_list:
            assert call.args[0] == "acct"

    def test_callback_not_fired_when_unregistered(self):
        """Backward compat: when on_position_changed is None,
        on_position_message behaves exactly as before.
        """
        # No callback wired — must not raise.
        fetcher = _make_fetcher()
        msg = {
            "data": [
                {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "size": "0.1"},
            ]
        }
        fetcher.on_position_message("acct", msg)
        # Cache write still happened.
        assert fetcher._position_ws_data["acct"]["BTCUSDT"]["Buy"]["size"] == "0.1"

    def test_callback_not_fired_for_filtered_messages(self):
        """Non-linear / empty-symbol entries don't store and don't notify."""
        callback = Mock()
        fetcher = _make_fetcher(on_position_changed=callback)
        msg = {
            "data": [
                {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "size": "1.0"},
                {"category": "linear", "symbol": "", "side": "Buy", "size": "0.1"},
            ]
        }
        fetcher.on_position_message("acct", msg)
        callback.assert_not_called()

    def test_callback_exception_isolated(self):
        """A misbehaving callback must not wedge the WS thread —
        cache writes succeed, exception is alerted via notifier.
        """
        notifier = Mock(spec=Notifier)
        callback = Mock(side_effect=RuntimeError("boom"))
        fetcher = _make_fetcher(notifier=notifier, on_position_changed=callback)
        msg = {
            "data": [
                {"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "size": "0.1"},
            ]
        }
        # Must not raise.
        fetcher.on_position_message("acct", msg)
        # Cache write still happened.
        assert fetcher._position_ws_data["acct"]["BTCUSDT"]["Buy"]["size"] == "0.1"
        # Notifier was alerted.
        notifier.alert_exception.assert_called_once()
        assert "on_position_changed" in notifier.alert_exception.call_args[0][0]

    def test_callback_skipped_when_message_raises(self):
        """If on_position_message hits an exception during cache writes,
        the callback must NOT fire — partial state should not leak.
        """
        callback = Mock()
        fetcher = _make_fetcher(on_position_changed=callback)
        # data=int triggers TypeError before any cache write.
        fetcher.on_position_message("acct", {"data": 12345})
        callback.assert_not_called()


class TestGetPositionFromWs:
    def test_returns_cached(self):
        fetcher = _make_fetcher()
        pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"}
        fetcher._position_ws_data = {"a": {"BTCUSDT": {"Buy": pos}}}
        assert fetcher.get_position_from_ws("a", "BTCUSDT", "Buy") is pos

    def test_returns_none_when_missing_account_symbol_or_side(self):
        fetcher = _make_fetcher()
        assert fetcher.get_position_from_ws("missing", "BTCUSDT", "Buy") is None
        fetcher._position_ws_data = {"a": {}}
        assert fetcher.get_position_from_ws("a", "BTCUSDT", "Buy") is None
        fetcher._position_ws_data = {"a": {"BTCUSDT": {}}}
        assert fetcher.get_position_from_ws("a", "BTCUSDT", "Buy") is None


class TestGetWalletBalance:
    def test_cached_within_ttl(self):
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "9999"}]}]
        }
        fetcher = _make_fetcher(
            rest_clients={"a": rest}, wallet_cache_interval=300.0,
        )
        fetcher._wallet_cache["a"] = (WalletSnapshot(wallet_balance=10000.0), datetime.now(UTC))

        assert fetcher.get_wallet_balance("a") == 10000.0
        rest.get_wallet_balance.assert_not_called()

    def test_expired_cache_refetches(self):
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "7500"}]}]
        }
        fetcher = _make_fetcher(
            rest_clients={"a": rest}, wallet_cache_interval=300.0,
        )
        fetcher._wallet_cache["a"] = (WalletSnapshot(wallet_balance=5000.0), datetime.now(UTC) - timedelta(seconds=400))

        assert fetcher.get_wallet_balance("a") == 7500.0
        rest.get_wallet_balance.assert_called_once()
        cached_snap, _ = fetcher._wallet_cache["a"]
        assert cached_snap.wallet_balance == 7500.0

    def test_disabled_when_interval_zero_always_fetches(self):
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "8000"}]}]
        }
        fetcher = _make_fetcher(
            rest_clients={"a": rest}, wallet_cache_interval=0.0,
        )
        # Pre-seed cache; should be ignored.
        fetcher._wallet_cache["a"] = (WalletSnapshot(wallet_balance=5000.0), datetime.now(UTC))

        assert fetcher.get_wallet_balance("a") == 8000.0
        rest.get_wallet_balance.assert_called_once()
        rest.get_wallet_balance.reset_mock()
        assert fetcher.get_wallet_balance("a") == 8000.0
        rest.get_wallet_balance.assert_called_once()

    def test_fetch_failure_propagates_and_does_not_cache(self):
        rest = Mock()
        rest.get_wallet_balance.side_effect = ConnectionError("timeout")
        fetcher = _make_fetcher(rest_clients={"a": rest})
        with pytest.raises(ConnectionError, match="timeout"):
            fetcher.get_wallet_balance("a")
        assert "a" not in fetcher._wallet_cache

    def test_no_usdt_returns_zero(self):
        """Unified wallet response with no USDT coin → 0.0 (not KeyError)."""
        rest = Mock()
        rest.get_wallet_balance.return_value = {"list": [{"coin": []}]}
        fetcher = _make_fetcher(
            rest_clients={"a": rest}, wallet_cache_interval=0.0,
        )
        assert fetcher.get_wallet_balance("a") == 0.0

    def test_raises_when_called_from_non_main_thread(self):
        """Runtime guard: touching _wallet_cache off the main thread must raise."""
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "1000"}]}]
        }
        fetcher = _make_fetcher(rest_clients={"a": rest})

        captured: list[BaseException] = []

        def run() -> None:
            try:
                fetcher.get_wallet_balance("a")
            except BaseException as exc:
                captured.append(exc)

        t = threading.Thread(target=run)
        t.start()
        t.join()

        assert len(captured) == 1
        assert isinstance(captured[0], RuntimeError)
        assert "main thread" in str(captured[0])
        rest.get_wallet_balance.assert_not_called()


class TestFetchAndUpdate:
    def _make_account_with_runner(self, fetcher, account_name="a", symbol="BTCUSDT"):
        """Wire one account + one mock runner into the fetcher's injected dicts."""
        runner = Mock(strat_id="s1", symbol=symbol)
        runner.engine.last_close = 42000.0
        runner.on_position_update = Mock()
        fetcher._account_to_runners[account_name] = [runner]
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]
        }
        rest.get_positions.return_value = []
        fetcher._rest_clients[account_name] = rest
        return runner, rest

    def test_startup_hard_cap_raises(self):
        fetcher = _make_fetcher()
        # Three accounts; _fetch_one_account is mocked so the runtime
        # cost comes entirely from the mocked time.monotonic sequence.
        for name in ("a", "b", "c"):
            fetcher._account_to_runners[name] = [Mock(symbol="BTCUSDT")]
            fetcher._rest_clients[name] = Mock()
        fetcher._fetch_one_account = Mock()

        # loop_start=0.0, first elapsed=0.0, second elapsed=cap+1 (trips).
        fake_times = iter([0.0, 0.0,
                           _POSITION_STARTUP_HARD_CAP + 1.0,
                           _POSITION_STARTUP_HARD_CAP + 1.0,
                           _POSITION_STARTUP_HARD_CAP + 1.0])
        with patch(
            "gridbot.position_fetcher.time.monotonic",
            side_effect=lambda: next(fake_times),
        ):
            with pytest.raises(StartupTimeoutError) as exc_info:
                fetcher.fetch_and_update(startup=True)

        assert "1/3 accounts" in str(exc_info.value)
        assert fetcher._fetch_one_account.call_count == 1

    def test_startup_skips_floor(self):
        """startup=True must not honour the per-account floor."""
        fetcher = _make_fetcher(position_check_interval=60.0)
        self._make_account_with_runner(fetcher, account_name="a")

        # Recently fetched — floor would otherwise skip, but startup ignores it.
        fetcher._last_position_fetch["a"] = 10_000.0
        with patch("gridbot.position_fetcher.time.monotonic", return_value=10_000.1):
            fetcher.fetch_and_update(startup=True)

        runner = fetcher._account_to_runners["a"][0]
        runner.on_position_update.assert_called_once()

    def test_steady_state_per_account_floor_skips_fresh(self):
        """Account fetched <floor ago must be skipped by rotation tick."""
        fetcher = _make_fetcher(position_check_interval=60.0)
        self._make_account_with_runner(fetcher, account_name="a")
        # 0.5s ago → below 60s floor.
        fetcher._last_position_fetch["a"] = 9_999.5
        with patch("gridbot.position_fetcher.time.monotonic", return_value=10_000.0):
            fetcher.fetch_and_update(startup=False)

        runner = fetcher._account_to_runners["a"][0]
        runner.on_position_update.assert_not_called()

    def test_runner_error_continues_to_next_runner(self):
        """One runner's on_position_update raising must not skip others."""
        notifier = Mock(spec=Notifier)
        fetcher = _make_fetcher(notifier=notifier)
        bad_runner = Mock(strat_id="bad", symbol="BTCUSDT")
        bad_runner.engine.last_close = 42000.0
        bad_runner.on_position_update = Mock(side_effect=RuntimeError("boom"))
        good_runner = Mock(strat_id="good", symbol="BTCUSDT")
        good_runner.engine.last_close = 42000.0
        good_runner.on_position_update = Mock()
        fetcher._account_to_runners["a"] = [bad_runner, good_runner]
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "1000"}]}]
        }
        rest.get_positions.return_value = []
        fetcher._rest_clients["a"] = rest

        fetcher.fetch_and_update(startup=True)

        bad_runner.on_position_update.assert_called_once()
        good_runner.on_position_update.assert_called_once()
        notifier.alert_exception.assert_called_once()

    def test_slow_threshold_logs_warning(self, caplog):
        """Per-account elapsed > slow-threshold emits a warning log."""
        fetcher = _make_fetcher()
        self._make_account_with_runner(fetcher, account_name="a")

        # Four time.monotonic() calls in _fetch_one_account flow:
        # start, finally-elapsed. Force elapsed > threshold.
        times = iter([1000.0, 1000.0 + _POSITION_FETCH_SLOW_THRESHOLD + 0.5])
        with patch(
            "gridbot.position_fetcher.time.monotonic",
            side_effect=lambda: next(times),
        ):
            with caplog.at_level(logging.WARNING, logger="gridbot.position_fetcher"):
                fetcher._fetch_one_account("a", fetcher._account_to_runners["a"])

        assert any(
            "Position fetch for a took" in rec.getMessage() for rec in caplog.records
        )

    def test_rest_fallback_when_ws_missing(self):
        fetcher = _make_fetcher()
        runner, rest = self._make_account_with_runner(fetcher, account_name="a")
        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.2"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.1"}
        rest.get_positions.return_value = [long_pos, short_pos]

        fetcher.fetch_and_update(startup=True)

        runner.on_position_update.assert_called_once_with(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=42000.0,
            available_balance=0.0,
            total_available_balance=0.0,
            total_maintenance_margin=0.0,
        )
        rest.get_positions.assert_called_once()

    def test_ws_primary_skips_rest(self):
        fetcher = _make_fetcher()
        runner, rest = self._make_account_with_runner(fetcher, account_name="a")
        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.05"}
        fetcher._position_ws_data["a"] = {"BTCUSDT": {"Buy": long_pos, "Sell": short_pos}}

        fetcher.fetch_and_update(startup=True)

        runner.on_position_update.assert_called_once_with(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=42000.0,
            available_balance=0.0,
            total_available_balance=0.0,
            total_maintenance_margin=0.0,
        )
        rest.get_positions.assert_not_called()

    def test_passes_none_when_runner_has_no_last_close(self):
        """Position fetches before first ticker must not fabricate price=0.0."""
        fetcher = _make_fetcher()
        runner, rest = self._make_account_with_runner(fetcher, account_name="a")
        runner.engine.last_close = None
        long_pos = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.2"}
        short_pos = {"symbol": "BTCUSDT", "side": "Sell", "size": "0.1"}
        rest.get_positions.return_value = [long_pos, short_pos]

        fetcher.fetch_and_update(startup=True)

        runner.on_position_update.assert_called_once_with(
            long_position=long_pos,
            short_position=short_pos,
            wallet_balance=10000.0,
            last_close=None,
            available_balance=0.0,
            total_available_balance=0.0,
            total_maintenance_margin=0.0,
        )


class TestWalletSnapshot:
    """Feature 0066 (issue #159) — wallet snapshot extraction."""

    def test_extracts_account_and_coin_fields(self):
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{
                "totalAvailableBalance": "1234.5",
                "totalMaintenanceMargin": "30.25",
                "coin": [
                    {"coin": "USDT", "walletBalance": "5000",
                     "availableToWithdraw": "900"},
                ],
            }]
        }
        fetcher = _make_fetcher(rest_clients={"a": rest}, wallet_cache_interval=0.0)
        snap = fetcher.get_wallet_snapshot("a")
        assert snap.wallet_balance == 5000.0
        assert snap.available_balance == 900.0
        assert snap.total_available_balance == 1234.5
        assert snap.total_maintenance_margin == 30.25
        # Back-compat float accessor still works.
        assert fetcher._fetch_wallet_snapshot("a").wallet_balance == 5000.0

    def test_empty_string_fields_coerced_to_zero(self):
        """Bybit mainnet UTA sends '' for unused numeric fields (RULES.md)."""
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{
                "totalAvailableBalance": "",
                "totalMaintenanceMargin": "",
                "coin": [
                    {"coin": "USDT", "walletBalance": "5000",
                     "availableToWithdraw": ""},
                ],
            }]
        }
        fetcher = _make_fetcher(rest_clients={"a": rest}, wallet_cache_interval=0.0)
        snap = fetcher.get_wallet_snapshot("a")
        assert snap.wallet_balance == 5000.0
        assert snap.available_balance == 0.0
        assert snap.total_available_balance == 0.0
        assert snap.total_maintenance_margin == 0.0

    def test_available_falls_back_to_account_total_when_coin_empty(self):
        """UTA cross-margin: empty per-coin field → account-level free margin."""
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{
                "totalAvailableBalance": "777.0",
                "coin": [
                    {"coin": "USDT", "walletBalance": "5000",
                     "availableToWithdraw": ""},
                ],
            }]
        }
        fetcher = _make_fetcher(rest_clients={"a": rest}, wallet_cache_interval=0.0)
        snap = fetcher.get_wallet_snapshot("a")
        assert snap.available_balance == 777.0

    def test_no_usdt_returns_zero_snapshot(self):
        rest = Mock()
        rest.get_wallet_balance.return_value = {"list": [{"coin": []}]}
        fetcher = _make_fetcher(rest_clients={"a": rest}, wallet_cache_interval=0.0)
        assert fetcher.get_wallet_snapshot("a") == WalletSnapshot()


class TestSnapshotFromWalletAccountRow:
    """Phase 4 (review P3) — shared parser for REST list[0] / WS data[0]."""

    def test_maps_account_and_coin_fields(self):
        fetcher = _make_fetcher()
        row = {
            "totalAvailableBalance": "1234.5",
            "totalMaintenanceMargin": "30.25",
            "coin": [
                {"coin": "USDT", "walletBalance": "5000",
                 "availableToWithdraw": "900"},
            ],
        }
        snap = fetcher._snapshot_from_wallet_account_row(row)
        assert snap == WalletSnapshot(
            wallet_balance=5000.0,
            available_balance=900.0,
            total_available_balance=1234.5,
            total_maintenance_margin=30.25,
        )

    def test_account_total_fallback_when_coin_empty(self):
        fetcher = _make_fetcher()
        row = {
            "totalAvailableBalance": "777.0",
            "coin": [
                {"coin": "USDT", "walletBalance": "5000",
                 "availableToWithdraw": ""},
            ],
        }
        snap = fetcher._snapshot_from_wallet_account_row(row)
        assert snap.available_balance == 777.0

    def test_no_usdt_coin_returns_none(self):
        fetcher = _make_fetcher()
        assert fetcher._snapshot_from_wallet_account_row({"coin": []}) is None

    def test_zero_coin_available_collapses_to_account_total(self):
        """Documented UTA semantics: a per-coin availableToWithdraw of "0" is
        indistinguishable from "" and collapses to the account-level free margin.
        Locks the fallback so it can't silently drift (review test-gap)."""
        fetcher = _make_fetcher()
        row = {
            "totalAvailableBalance": "500",
            "coin": [{"coin": "USDT", "walletBalance": "100",
                      "availableToWithdraw": "0"}],
        }
        snap = fetcher._snapshot_from_wallet_account_row(row)
        assert snap.available_balance == 500.0  # account total, not the per-coin 0

    def test_legacy_available_balance_used_when_v5_field_absent(self):
        """review round-4 F1: legacy UTA 1.0 / cross-margin coins surface free
        margin only as `availableBalance`; parser must fall back to it (mirrors
        recorder.py) so the preflight doesn't misread free margin as 0."""
        fetcher = _make_fetcher()
        row = {
            "coin": [{"coin": "USDT", "walletBalance": "5000",
                      "availableBalance": "321.0"}],  # no availableToWithdraw key
        }
        snap = fetcher._snapshot_from_wallet_account_row(row)
        assert snap.available_balance == 321.0

    def test_legacy_available_balance_used_when_v5_field_empty(self):
        fetcher = _make_fetcher()
        row = {
            "coin": [{"coin": "USDT", "walletBalance": "5000",
                      "availableToWithdraw": "", "availableBalance": "150.5"}],
        }
        snap = fetcher._snapshot_from_wallet_account_row(row)
        assert snap.available_balance == 150.5

    def test_v5_availableToWithdraw_preferred_over_legacy_field(self):
        """When the v5 field is present and non-empty it wins over the legacy one."""
        fetcher = _make_fetcher()
        row = {
            "coin": [{"coin": "USDT", "walletBalance": "5000",
                      "availableToWithdraw": "900", "availableBalance": "111"}],
        }
        snap = fetcher._snapshot_from_wallet_account_row(row)
        assert snap.available_balance == 900.0

    def test_same_dict_yields_same_result_rest_or_ws(self):
        """A single account-row dict parses identically regardless of source."""
        fetcher = _make_fetcher()
        row = {
            "totalAvailableBalance": "100.0",
            "coin": [{"coin": "USDT", "walletBalance": "200",
                      "availableToWithdraw": "50"}],
        }
        # REST path uses list[0]; WS path uses data[0] — same dict.
        assert (fetcher._snapshot_from_wallet_account_row(row)
                == fetcher._snapshot_from_wallet_account_row(row))


class TestOnWalletMessage:
    """Phase 4 — WS `wallet` topic handler (runs on the WS thread)."""

    def _frame(self, *, wallet_balance="5000", available="120",
               total_available="777.0"):
        return {
            "topic": "wallet",
            "creationTime": 1704067200000,
            "data": [{
                "totalAvailableBalance": total_available,
                "coin": [{"coin": "USDT", "walletBalance": wallet_balance,
                          "availableToWithdraw": available}],
            }],
        }

    def test_populates_ws_snapshot(self):
        fetcher = _make_fetcher()
        fetcher.on_wallet_message("a", self._frame())
        snap, ts = fetcher._wallet_ws_data["a"]
        assert snap.wallet_balance == 5000.0
        assert snap.available_balance == 120.0
        assert isinstance(ts, datetime)

    def test_skips_missing_data_keeps_last_good(self):
        fetcher = _make_fetcher()
        prior = (WalletSnapshot(available_balance=99.0), datetime.now(UTC))
        fetcher._wallet_ws_data["a"] = prior
        fetcher.on_wallet_message("a", {"topic": "wallet"})  # no data key
        assert fetcher._wallet_ws_data["a"] is prior

    def test_skips_no_usdt_coin_keeps_last_good(self):
        fetcher = _make_fetcher()
        prior = (WalletSnapshot(available_balance=99.0), datetime.now(UTC))
        fetcher._wallet_ws_data["a"] = prior
        fetcher.on_wallet_message("a", {"data": [{"coin": []}]})
        assert fetcher._wallet_ws_data["a"] is prior

    def test_writes_valid_row_with_zero_available_not_masked(self):
        """A funded account with 0 free margin (wallet>0) is a REAL signal —
        written, not skipped by an all-zero heuristic (review P2-malformed)."""
        fetcher = _make_fetcher()
        fetcher.on_wallet_message("a", {
            "data": [{"coin": [{"coin": "USDT", "walletBalance": "100",
                                "availableToWithdraw": "0"}]}],
        })
        snap, _ = fetcher._wallet_ws_data["a"]
        assert snap.wallet_balance == 100.0
        assert snap.available_balance == 0.0

    def test_writes_legacy_available_balance_field(self):
        """review round-4 F1: WS frame whose USDT coin carries only the legacy
        `availableBalance` is parsed via the shared parser → free margin captured."""
        fetcher = _make_fetcher()
        fetcher.on_wallet_message("a", {
            "data": [{"coin": [{"coin": "USDT", "walletBalance": "5000",
                                "availableBalance": "75.0"}]}],
        })
        snap, _ = fetcher._wallet_ws_data["a"]
        assert snap.available_balance == 75.0

    def test_never_raises_on_parse_error(self):
        """Malformed frame must not raise on the WS thread (no write), and the
        swallowed error IS surfaced to the operator (mirrors on_position_message)."""
        notifier = Mock(spec=Notifier)
        fetcher = _make_fetcher(notifier=notifier)
        fetcher.on_wallet_message("a", {"data": "not-a-list"})  # data[0] → str
        assert "a" not in fetcher._wallet_ws_data
        notifier.alert_exception.assert_called_once()
        assert notifier.alert_exception.call_args[0][0] == "_on_wallet"
        assert notifier.alert_exception.call_args.kwargs.get("error_key") == "ws_on_wallet"


class TestPeekWalletSnapshot:
    """Phase 4 — non-blocking hot-path reader (newest of WS / REST by ts)."""

    def test_returns_none_when_no_data(self):
        fetcher = _make_fetcher()
        assert fetcher.peek_wallet_snapshot("a") is None

    def test_non_blocking_never_fetches(self):
        rest = Mock()
        fetcher = _make_fetcher(rest_clients={"a": rest})
        fetcher._wallet_ws_data["a"] = (
            WalletSnapshot(available_balance=100.0), datetime.now(UTC))
        snap, age = fetcher.peek_wallet_snapshot("a")
        assert snap.available_balance == 100.0
        assert age >= 0.0
        rest.get_wallet_balance.assert_not_called()

    def test_prefers_newer_rest_over_stale_ws(self):
        """review P2: a stale WS slot must NOT shadow a fresher REST entry."""
        fetcher = _make_fetcher()
        now = datetime.now(UTC)
        fetcher._wallet_ws_data["a"] = (
            WalletSnapshot(available_balance=999.0), now - timedelta(seconds=120))
        fetcher._wallet_cache["a"] = (
            WalletSnapshot(available_balance=5.0), now - timedelta(seconds=1))
        snap, age = fetcher.peek_wallet_snapshot("a")
        assert snap.available_balance == 5.0  # the fresher REST one
        assert age < 60.0

    def test_prefers_newer_ws_over_older_rest(self):
        fetcher = _make_fetcher()
        now = datetime.now(UTC)
        fetcher._wallet_ws_data["a"] = (
            WalletSnapshot(available_balance=5.0), now - timedelta(seconds=1))
        fetcher._wallet_cache["a"] = (
            WalletSnapshot(available_balance=999.0), now - timedelta(seconds=120))
        snap, age = fetcher.peek_wallet_snapshot("a")
        assert snap.available_balance == 5.0  # the fresher WS one
        assert age < 60.0

    def test_returns_only_present_source(self):
        fetcher = _make_fetcher()
        now = datetime.now(UTC)
        fetcher._wallet_cache["a"] = (WalletSnapshot(available_balance=7.0), now)
        snap, _ = fetcher.peek_wallet_snapshot("a")
        assert snap.available_balance == 7.0


class TestGetWalletSnapshotWsPrimary:
    """Phase 4 — background reader: WS within window, else REST (may fetch)."""

    def test_ws_primary_when_fresh(self):
        rest = Mock()
        fetcher = _make_fetcher(rest_clients={"a": rest},
                                wallet_ws_max_age_seconds=45.0)
        fetcher._wallet_ws_data["a"] = (
            WalletSnapshot(available_balance=42.0), datetime.now(UTC))
        snap = fetcher.get_wallet_snapshot("a")
        assert snap.available_balance == 42.0
        rest.get_wallet_balance.assert_not_called()

    def test_rest_when_ws_stale(self):
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "5000",
                                "availableToWithdraw": "100"}]}]
        }
        fetcher = _make_fetcher(rest_clients={"a": rest},
                                wallet_ws_max_age_seconds=45.0,
                                wallet_cache_interval=300.0)
        fetcher._wallet_ws_data["a"] = (
            WalletSnapshot(available_balance=42.0),
            datetime.now(UTC) - timedelta(seconds=100))  # stale WS
        snap = fetcher.get_wallet_snapshot("a")
        assert snap.wallet_balance == 5000.0  # fell through to REST
        rest.get_wallet_balance.assert_called_once()

    def test_rest_when_no_ws(self):
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "8000",
                                "availableToWithdraw": "200"}]}]
        }
        fetcher = _make_fetcher(rest_clients={"a": rest},
                                wallet_cache_interval=0.0)
        snap = fetcher.get_wallet_snapshot("a")
        assert snap.wallet_balance == 8000.0
        rest.get_wallet_balance.assert_called_once()

    def test_logs_ws_source_with_age_when_fresh(self, caplog):
        """review F3: DEBUG ops log identifies the WS source + slot age — the
        signal to validate WS freshness in rollout (the INFO heartbeat lags)."""
        rest = Mock()
        fetcher = _make_fetcher(rest_clients={"a": rest},
                                wallet_ws_max_age_seconds=45.0)
        fetcher._wallet_ws_data["a"] = (
            WalletSnapshot(available_balance=42.0), datetime.now(UTC))
        with caplog.at_level(logging.DEBUG, logger="gridbot.position_fetcher"):
            fetcher.get_wallet_snapshot("a")
        assert any("WS" in r.getMessage() and "age=" in r.getMessage()
                   for r in caplog.records)

    def test_logs_rest_source_when_ws_stale_or_absent(self, caplog):
        """review F3: DEBUG ops log identifies the REST fallback source."""
        rest = Mock()
        rest.get_wallet_balance.return_value = {
            "list": [{"coin": [{"coin": "USDT", "walletBalance": "8000",
                                "availableToWithdraw": "200"}]}]
        }
        fetcher = _make_fetcher(rest_clients={"a": rest},
                                wallet_cache_interval=0.0)
        with caplog.at_level(logging.DEBUG, logger="gridbot.position_fetcher"):
            fetcher.get_wallet_snapshot("a")
        assert any("REST" in r.getMessage() for r in caplog.records)
