"""Isolated unit tests for AuthCooldownManager.

These tests exercise AuthCooldownManager directly, without spinning up
an Orchestrator. They complement the integration-style tests in
test_orchestrator.py (which go through the Orchestrator and assert the
callback and health-check wiring).
"""

import threading
from datetime import datetime, timedelta, UTC
from unittest.mock import Mock

from gridbot.auth_cooldown_manager import AuthCooldownManager
from gridbot.notifier import Notifier


def _make_manager(
    *,
    strategy_executors=None,
    retry_queues=None,
    notifier=None,
    cooldown_minutes=5,
):
    return AuthCooldownManager(
        strategy_executors=strategy_executors if strategy_executors is not None else {},
        retry_queues=retry_queues if retry_queues is not None else {},
        notifier=notifier if notifier is not None else Mock(spec=Notifier),
        cooldown_minutes=cooldown_minutes,
    )


class TestEnter:
    def test_increments_cycle_counter(self):
        executor = Mock()
        executor.auth_failure_count = 5
        manager = _make_manager(strategy_executors={"s1": executor})

        manager.enter("s1")
        assert manager._auth_cooldown_cycles["s1"] == 1

        manager.enter("s1")
        assert manager._auth_cooldown_cycles["s1"] == 2

    def test_sets_expiry_based_on_cooldown_minutes(self):
        manager = _make_manager(cooldown_minutes=10)

        before = datetime.now(UTC)
        manager.enter("s1")
        after = datetime.now(UTC)

        expiry = manager._auth_cooldown_until["s1"]
        assert expiry >= before + timedelta(minutes=10)
        assert expiry <= after + timedelta(minutes=10)

    def test_clears_retry_queue(self):
        retry_queue = Mock()
        retry_queue.clear.return_value = 2
        manager = _make_manager(retry_queues={"s1": retry_queue})

        manager.enter("s1")

        retry_queue.clear.assert_called_once()

    def test_alerts_notifier_with_cycle_number_and_key(self):
        notifier = Mock(spec=Notifier)
        executor = Mock()
        executor.auth_failure_count = 3
        manager = _make_manager(
            strategy_executors={"s1": executor}, notifier=notifier,
        )

        manager.enter("s1")

        notifier.alert.assert_called_once()
        body = notifier.alert.call_args[0][0]
        assert "cycle 1" in body
        assert "3 consecutive auth errors" in body
        assert notifier.alert.call_args.kwargs["error_key"] == "auth_cooldown_s1"

    def test_missing_executor_falls_back_to_question_mark(self):
        notifier = Mock(spec=Notifier)
        manager = _make_manager(notifier=notifier)  # empty executor dict

        manager.enter("s1")

        body = notifier.alert.call_args[0][0]
        assert "? consecutive auth errors" in body

    def test_raises_when_called_from_non_main_thread(self):
        """Fail-loud guard: touching state off the main thread must raise."""
        manager = _make_manager()

        captured: list[BaseException] = []

        def run() -> None:
            try:
                manager.enter("s1")
            except BaseException as exc:
                captured.append(exc)

        t = threading.Thread(target=run, name="worker-42")
        t.start()
        t.join()

        assert len(captured) == 1
        assert isinstance(captured[0], RuntimeError)
        # Documented prefix — guards against accidental message drift that
        # would otherwise slip through looser substring checks.
        assert str(captured[0]).startswith(
            "AuthCooldownManager.enter() must run on the main thread"
        )
        assert "worker-42" in str(captured[0])
        assert "s1" not in manager._auth_cooldown_until
        assert "s1" not in manager._auth_cooldown_cycles


class TestSweepExpired:
    def test_removes_past_expiries_and_keeps_future(self):
        executor_a = Mock()
        executor_b = Mock()
        manager = _make_manager(
            strategy_executors={"a": executor_a, "b": executor_b},
        )
        now = datetime.now(UTC)
        manager._auth_cooldown_until["a"] = now - timedelta(seconds=1)
        manager._auth_cooldown_until["b"] = now + timedelta(minutes=5)
        manager._auth_cooldown_cycles["a"] = 1
        manager._auth_cooldown_cycles["b"] = 1

        manager.sweep_expired(now)

        assert "a" not in manager._auth_cooldown_until
        assert "b" in manager._auth_cooldown_until
        # Cycle history is cumulative — not cleared on expiry.
        assert manager._auth_cooldown_cycles["a"] == 1

    def test_calls_reset_on_executor(self):
        executor = Mock()
        manager = _make_manager(strategy_executors={"s1": executor})
        now = datetime.now(UTC)
        manager._auth_cooldown_until["s1"] = now - timedelta(seconds=1)
        manager._auth_cooldown_cycles["s1"] = 2

        manager.sweep_expired(now)

        executor.reset_auth_cooldown.assert_called_once()

    def test_missing_executor_still_deletes_entry(self):
        # Defensive: dynamic strategy removal could drop the executor
        # mid-flight. Expiry sweep must still reclaim the entry.
        manager = _make_manager()  # empty executor dict
        now = datetime.now(UTC)
        manager._auth_cooldown_until["s1"] = now - timedelta(seconds=1)

        manager.sweep_expired(now)

        assert "s1" not in manager._auth_cooldown_until

    def test_alerts_with_resume_message_and_key(self):
        notifier = Mock(spec=Notifier)
        executor = Mock()
        manager = _make_manager(
            strategy_executors={"s1": executor}, notifier=notifier,
        )
        now = datetime.now(UTC)
        manager._auth_cooldown_until["s1"] = now - timedelta(seconds=1)
        manager._auth_cooldown_cycles["s1"] = 3

        manager.sweep_expired(now)

        notifier.alert.assert_called_once()
        body = notifier.alert.call_args[0][0]
        assert "cooldown expired" in body
        assert "cycle 3" in body
        assert notifier.alert.call_args.kwargs["error_key"] == "auth_cooldown_resume_s1"

    def test_skips_entries_not_yet_expired(self):
        executor = Mock()
        notifier = Mock(spec=Notifier)
        manager = _make_manager(
            strategy_executors={"s1": executor}, notifier=notifier,
        )
        now = datetime.now(UTC)
        manager._auth_cooldown_until["s1"] = now + timedelta(minutes=5)

        manager.sweep_expired(now)

        executor.reset_auth_cooldown.assert_not_called()
        notifier.alert.assert_not_called()
        assert "s1" in manager._auth_cooldown_until
