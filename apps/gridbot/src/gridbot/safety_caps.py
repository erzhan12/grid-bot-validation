"""Production safety caps (feature 0079 / issue #182).

Hard, last-resort caps enforced OUTSIDE strategy logic so they cannot be
overridden by grid-engine decisions. ``SafetyCaps`` owns all cap state and
decision logic in ONE place (design goal: caps enforced in one place, not
duplicated per strategy). It is pure: no network/DB imports. The orchestrator
constructs a single instance per strategy and passes the SAME object to both
the ``StrategyRunner`` (C1/C2 open-guard + C3 loss breaker) and the
``IntentExecutor`` (C4 rate limit) so the C4 window and the loss latch are one
source of truth.

Four caps:
- C1 max notional exposure per symbol — suppresses new OPENs; closes exempt.
- C2 max open orders per strat — a pure count limit; blocks open AND close.
- C3 session realized-loss circuit breaker — latches on trip; caller cancels
  working orders once and then suppresses all places until recovery.
- C4 max orders per minute — trailing-60s rate limit at the executor.

Clock contract: the constructor takes ONLY an injectable monotonic
``clock: Callable[[], float]`` (default ``time.monotonic``) used for the C4
window. There is NO wall-clock/UTC source at ``__init__``. UTC enters only via
the ``now_utc`` argument to :meth:`check_loss_breaker`; the C3 reset date is
seeded lazily on the first such call.

Shadow-mode note: C1/C2/C3 run in the runner BEFORE the executor, so in shadow
mode a tripped cap still suppresses the "[SHADOW] Would place ..." log (the
faithful behavior). C4 lives at the executor AFTER its shadow early-return, so
shadow placements never consume the C4 window.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Optional

from gridbot.config import SafetyCapsConfig

logger = logging.getLogger(__name__)

# Trailing window length for the C4 rate limit, in seconds.
_RATE_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class CapDecision:
    """Outcome of a place-path cap check.

    ``reason`` is one of ``"max_notional"``, ``"max_open_orders"``,
    ``"loss_breaker"``, ``"rate_limit"`` or ``""`` (allowed). Callers map a
    non-empty reason to a throttled WARNING + ``notifier.alert(...,
    error_key=f"safety_cap_{reason}_{strat_id}")``.
    """

    allowed: bool
    reason: str


_ALLOWED = CapDecision(allowed=True, reason="")


class SafetyCaps:
    """Single source of truth for all production safety caps of one strategy."""

    def __init__(
        self,
        config: SafetyCapsConfig,
        strat_id: str,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Initialize.

        Args:
            config: The per-strategy ``SafetyCapsConfig``.
            strat_id: Strategy id, for log messages.
            clock: Monotonic clock for the C4 window; injectable for tests.
        """
        self._config = config
        self._strat_id = strat_id
        self._clock = clock

        # C3 latch state.
        self._loss_tripped: bool = False
        # UTC calendar date the latch is bound to. None until the first
        # check_loss_breaker seeds it lazily from that call's now_utc (the
        # constructor has no UTC source — see module docstring).
        self._loss_reset_utc_date: Optional[date] = None

        # C4 trailing-window of accepted-submission monotonic timestamps.
        self._rate_window: deque[float] = deque()

    def allow_open(
        self, *, total_notional: Decimal, open_order_count: int
    ) -> CapDecision:
        """C1 + C2 gate for an OPEN place intent.

        C1 (notional) is checked before C2 (count); the first failing cap is
        reported. Both are inert when the master switch is off or the cap value
        is ``None``.
        """
        if not self._config.enabled:
            return _ALLOWED
        cap_notional = self._config.max_notional_per_symbol
        if cap_notional is not None and total_notional >= cap_notional:
            return CapDecision(allowed=False, reason="max_notional")
        cap_orders = self._config.max_open_orders
        if cap_orders is not None and open_order_count >= cap_orders:
            return CapDecision(allowed=False, reason="max_open_orders")
        return _ALLOWED

    def allow_reduce_only(self, *, open_order_count: int) -> CapDecision:
        """C2 gate for a reduce-only place intent (C1 never blocks closes)."""
        if not self._config.enabled:
            return _ALLOWED
        cap_orders = self._config.max_open_orders
        if cap_orders is not None and open_order_count >= cap_orders:
            return CapDecision(allowed=False, reason="max_open_orders")
        return _ALLOWED

    def check_loss_breaker(
        self, *, session_realized_pnl: Decimal, now_utc: datetime
    ) -> bool:
        """C3 evaluation. Returns True only on the transition into a trip.

        Called from the position-update path (where realized PnL lands), not
        the place path. A True return means the caller should cancel working
        orders once and alert. ``loss_tripped()`` is the cheap read for the
        place path thereafter.
        """
        if not self._config.enabled:
            return False
        cap = self._config.session_loss_limit
        if cap is None:
            return False

        # Seed-if-unset, then UTC-midnight auto-reset.
        if self._loss_reset_utc_date is None:
            self._loss_reset_utc_date = now_utc.date()
        if (
            self._config.session_loss_auto_reset_utc_midnight
            and now_utc.date() > self._loss_reset_utc_date
        ):
            self._loss_tripped = False
            self._loss_reset_utc_date = now_utc.date()
            logger.info(
                "%s: session-loss breaker auto-reset at UTC midnight (date=%s)",
                self._strat_id,
                self._loss_reset_utc_date,
            )

        if not self._loss_tripped and session_realized_pnl <= -cap:
            self._loss_tripped = True
            return True
        return False

    def loss_tripped(self) -> bool:
        """Cheap read for the place path: True while the C3 latch is set."""
        return self._loss_tripped

    def record_accepted_submission(self, now: float) -> None:
        """Append an accepted real submission to the C4 window."""
        if not self._config.enabled or self._config.max_orders_per_minute is None:
            return
        self._rate_window.append(now)

    def rate_limited(self, now: float) -> bool:
        """C4: evict the trailing-60s window, then True when count >= cap."""
        if not self._config.enabled:
            return False
        cap = self._config.max_orders_per_minute
        if cap is None:
            return False
        cutoff = now - _RATE_WINDOW_SECONDS
        while self._rate_window and self._rate_window[0] <= cutoff:
            self._rate_window.popleft()
        return len(self._rate_window) >= cap
