"""110017 circuit-breaker (feature 0064, issue #149).

Bounds the blast radius of an ErrCode 110017 ("orderQty will be truncated to
zero") retry storm. The engine re-emits the same logical reduce-only close on
(nearly) every ``on_ticker`` while price sits at a grid level; without a gate
that produced 535 identical rejected placements in <90min during the
2026-05-30 incident.

Design mirrors ``RetryQueue``: no background thread, no internal clock. The
owner (``StrategyRunner``) passes ``now`` into each call, so the breaker is
trivially testable with a virtual clock and free of ``time`` coupling.

Scope key is ``(side, price)`` — NOT ``orderLinkId`` (which carries a
per-placement ``-{millis}`` suffix and changes every retry, so it would never
accumulate). ``(side, price)`` is stable across per-tick re-emission of the
same grid-level close and matches the verbatim rejected payload identity.
``reduce_only`` is implicit: 110017 only occurs on reduce-only orders.
"""

import logging
from collections import deque
from decimal import Decimal
from typing import Deque, Dict, Tuple


logger = logging.getLogger(__name__)

# (side, price) — price kept as Decimal so equal values of differing scale
# (84.5 == 84.50) collide on one key (Python guarantees equal Decimals hash
# equal).
ScopeKey = Tuple[str, Decimal]


class TruncateBreaker:
    """Sliding-window trip/cooldown gate keyed by ``(side, price)``."""

    def __init__(
        self,
        max_consecutive: int = 3,
        window_seconds: float = 60.0,
        cooldown_seconds: float = 60.0,
    ):
        """Initialize the breaker.

        Args:
            max_consecutive: Trip after this many 110017s within ``window_seconds``.
            window_seconds: Sliding window for counting events per scope key.
            cooldown_seconds: After a trip, ``is_blocked`` returns True for this
                long on that scope key.
        """
        self._max = max_consecutive
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._events: Dict[ScopeKey, Deque[float]] = {}
        self._tripped_until: Dict[ScopeKey, float] = {}

    @staticmethod
    def _key(side: str, price: Decimal) -> ScopeKey:
        return (side, price)

    def is_blocked(self, side: str, price: Decimal, now: float) -> bool:
        """True if this scope key is in an active cooldown (drop the intent).

        On cooldown expiry, clears the trip and the timestamp deque (fresh
        start) and returns False.
        """
        key = self._key(side, price)
        until = self._tripped_until.get(key)
        if until is None:
            return False
        if now < until:
            return True
        # Cooldown expired: reset this scope key entirely.
        del self._tripped_until[key]
        self._events.pop(key, None)
        return False

    def record_110017(self, side: str, price: Decimal, now: float) -> bool:
        """Record one 110017; return True only on the first-trip edge.

        - In-cooldown no-op (P2-a): if the scope key is currently tripped, return
          False WITHOUT appending, re-arming the trip, or signalling reconcile.
          A tripped scope stays silently blocked until cooldown expiry.
        - Otherwise append ``now``, evict timestamps older than the window, and
          trip (return True) when the count reaches ``max_consecutive``.
        """
        key = self._key(side, price)
        until = self._tripped_until.get(key)
        if until is not None:
            if now < until:
                return False  # in-cooldown no-op
            # Expired trip lingering (is_blocked normally clears it first): reset.
            del self._tripped_until[key]
            self._events.pop(key, None)

        dq = self._events.setdefault(key, deque())
        dq.append(now)
        cutoff = now - self._window
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) >= self._max:
            self._tripped_until[key] = now + self._cooldown
            return True
        return False

    def record_success(self, side: str, price: Decimal) -> None:
        """A successful place at this scope means divergence healed: reset it."""
        key = self._key(side, price)
        self._events.pop(key, None)
        self._tripped_until.pop(key, None)
