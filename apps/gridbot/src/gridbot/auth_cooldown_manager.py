"""Auth-cooldown state machine extracted from Orchestrator.

Owns the per-strategy cooldown expiry map and the cumulative cycle
counter. Callers (Orchestrator) hand in collaborators via constructor
injection — this class holds no back-reference to Orchestrator.

Thread model: main-thread-only. ``enter`` mutates ``_auth_cooldown_cycles``
(read-then-write — NOT atomic) and ``_auth_cooldown_until``, and calls
``retry_queue.clear()`` which runs in parallel with ``process_due()``
only if the main-thread assumption holds. All current callers satisfy
this: executor entry points (``execute_place`` / ``execute_cancel`` /
``execute_amend``) are invoked from ``StrategyRunner`` (main-thread
ticker cycle) and ``RetryQueue.process_due()`` (main-thread retry-drain
tick). ``enter`` enforces the invariant at runtime with a fail-loud
``RuntimeError``.

Design note: the fail-loud thread guard is deliberate — not a missing
lock. Adding one here would signal "safe from any thread" and invite
callers that deadlock against ``process_due`` or push us into a
drain-pattern that delays cooldown activation. Enforcing the invariant
at runtime keeps the design simple and makes any violation impossible
to miss.
"""

import logging
import threading
from datetime import datetime, timedelta, UTC

from gridbot.executor import IntentExecutor
from gridbot.notifier import Notifier
from gridbot.retry_queue import RetryQueue

logger = logging.getLogger(__name__)


class AuthCooldownManager:
    """Per-strategy auth-cooldown lifecycle (entry + expiry sweep).

    Owns two pieces of state: ``_auth_cooldown_until`` (expiry timestamp
    per strat_id) and ``_auth_cooldown_cycles`` (cumulative cooldown
    count per strat_id).
    """

    def __init__(
        self,
        *,
        strategy_executors: dict[str, IntentExecutor],
        retry_queues: dict[str, RetryQueue],
        notifier: Notifier,
        cooldown_minutes: int,
    ):
        # Dicts held by reference; Orchestrator mutates them in place
        # during _init_strategy, and those updates are visible here.
        self._strategy_executors = strategy_executors
        self._retry_queues = retry_queues
        self._notifier = notifier
        self._cooldown_minutes = cooldown_minutes

        self._auth_cooldown_until: dict[str, datetime] = {}  # strat_id -> expiry
        self._auth_cooldown_cycles: dict[str, int] = {}  # strat_id -> cumulative cycle count

    def enter(self, strat_id: str) -> None:
        """Called by executor when auth cooldown activates.

        Works regardless of whether the failure came from the ticker path
        or the retry queue path. See module docstring for the thread-model
        rationale and the fail-loud guard's role.
        """
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "_on_auth_cooldown_entered must run on the main thread; "
                "see docstring for the locking required before relaxing "
                f"this. Called from: {threading.current_thread().name}"
            )
        cycle = self._auth_cooldown_cycles.get(strat_id, 0) + 1
        self._auth_cooldown_cycles[strat_id] = cycle

        expiry = datetime.now(UTC) + timedelta(minutes=self._cooldown_minutes)
        self._auth_cooldown_until[strat_id] = expiry

        executor = self._strategy_executors.get(strat_id)
        failure_count = executor.auth_failure_count if executor else "?"

        # Clear retry queue — stale intents would fail with the same auth error,
        # and fresh intents at current prices will be generated after cooldown.
        retry_queue = self._retry_queues.get(strat_id)
        if retry_queue:
            cleared = retry_queue.clear()
            if cleared:
                logger.info(f"Cleared {cleared} items from retry queue for {strat_id}")

        msg = (
            f"Strategy {strat_id}: {failure_count} consecutive auth errors, "
            f"entering {self._cooldown_minutes}-min cooldown (cycle {cycle})"
        )
        logger.error(msg)
        self._notifier.alert(msg, error_key=f"auth_cooldown_{strat_id}")

    def sweep_expired(self, now: datetime) -> None:
        """Reset any strategies whose cooldown window has elapsed.

        Iterates over a ``list(keys())`` snapshot so ``del`` during the
        loop cannot raise ``RuntimeError: dict changed size during
        iteration``.
        """
        for strat_id in list(self._auth_cooldown_until.keys()):
            expiry = self._auth_cooldown_until[strat_id]
            if now >= expiry:
                executor = self._strategy_executors.get(strat_id)
                cycle = self._auth_cooldown_cycles.get(strat_id, 1)
                if executor:
                    executor.reset_auth_cooldown()
                    msg = (
                        f"Strategy {strat_id}: cooldown expired (cycle {cycle}), "
                        f"resuming order execution"
                    )
                    logger.info(msg)
                    self._notifier.alert(
                        msg, error_key=f"auth_cooldown_resume_{strat_id}",
                    )
                del self._auth_cooldown_until[strat_id]
