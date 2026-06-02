"""Tests for the 110017 circuit-breaker (feature 0064).

The breaker is poll-free and clock-injected: callers pass ``now`` to
``is_blocked`` / ``record_110017`` so tests drive a virtual clock directly.
Scope key is ``(side, price)`` — stable across the engine's per-tick
re-emission of the same grid-level reduce-only close.
"""

from decimal import Decimal

import pytest

from gridbot.truncate_breaker import TruncateBreaker


SELL = "Sell"
PRICE = Decimal("84.51")


@pytest.fixture
def breaker():
    return TruncateBreaker(
        max_consecutive=3,
        window_seconds=60.0,
        cooldown_seconds=60.0,
    )


def test_breaker_trips_after_n_within_window(breaker):
    assert breaker.record_110017(SELL, PRICE, now=0.0) is False
    assert breaker.record_110017(SELL, PRICE, now=1.0) is False
    # 3rd within window → trip (first-trip edge True)
    assert breaker.record_110017(SELL, PRICE, now=2.0) is True
    assert breaker.is_blocked(SELL, PRICE, now=3.0) is True


def test_breaker_does_not_trip_below_n(breaker):
    assert breaker.record_110017(SELL, PRICE, now=0.0) is False
    assert breaker.record_110017(SELL, PRICE, now=1.0) is False
    assert breaker.is_blocked(SELL, PRICE, now=2.0) is False


def test_breaker_no_retrip_during_cooldown(breaker):
    """P2-a: a scope already tripped stays silently blocked.

    record_110017 on an already-tripped scope returns False, does NOT append
    to the deque, does NOT re-arm the trip, and the caller must not re-fire
    reconcile (proven here by the stable return value).
    """
    breaker.record_110017(SELL, PRICE, now=0.0)
    breaker.record_110017(SELL, PRICE, now=1.0)
    assert breaker.record_110017(SELL, PRICE, now=2.0) is True  # trip
    # Further 110017s while tripped → no-op, return False
    assert breaker.record_110017(SELL, PRICE, now=3.0) is False
    assert breaker.record_110017(SELL, PRICE, now=30.0) is False
    # Still blocked through the original cooldown window (tripped at 2.0 + 60)
    assert breaker.is_blocked(SELL, PRICE, now=59.0) is True
    # The no-op record at 3.0/30.0 must NOT have extended cooldown past 62.0
    assert breaker.is_blocked(SELL, PRICE, now=62.5) is False


def test_breaker_window_eviction(breaker):
    """A slow drip outside the window never trips (old timestamps evicted)."""
    breaker.record_110017(SELL, PRICE, now=0.0)
    breaker.record_110017(SELL, PRICE, now=40.0)
    # 80.0 is >60 after the first event → first evicted; only 2 in window
    assert breaker.record_110017(SELL, PRICE, now=80.0) is False
    assert breaker.is_blocked(SELL, PRICE, now=81.0) is False


def test_breaker_cooldown_expiry_resets(breaker):
    breaker.record_110017(SELL, PRICE, now=0.0)
    breaker.record_110017(SELL, PRICE, now=1.0)
    assert breaker.record_110017(SELL, PRICE, now=2.0) is True  # trip at 2.0+60
    assert breaker.is_blocked(SELL, PRICE, now=61.0) is True
    # After cooldown, no longer blocked and the deque is cleared (fresh start)
    assert breaker.is_blocked(SELL, PRICE, now=62.1) is False
    # Fresh start: a single new 110017 does not immediately re-trip
    assert breaker.record_110017(SELL, PRICE, now=63.0) is False


def test_breaker_success_resets_scope_key(breaker):
    breaker.record_110017(SELL, PRICE, now=0.0)
    breaker.record_110017(SELL, PRICE, now=1.0)
    breaker.record_success(SELL, PRICE)
    # Deque cleared → next two 110017s do not trip on the 2nd
    assert breaker.record_110017(SELL, PRICE, now=2.0) is False
    assert breaker.record_110017(SELL, PRICE, now=3.0) is False
    assert breaker.is_blocked(SELL, PRICE, now=4.0) is False


def test_breaker_success_clears_active_trip(breaker):
    breaker.record_110017(SELL, PRICE, now=0.0)
    breaker.record_110017(SELL, PRICE, now=1.0)
    assert breaker.record_110017(SELL, PRICE, now=2.0) is True
    breaker.record_success(SELL, PRICE)
    assert breaker.is_blocked(SELL, PRICE, now=3.0) is False


def test_breaker_scope_keys_independent(breaker):
    """Tripping Sell@84.51 must not block Buy@84.51 or Sell@84.52."""
    breaker.record_110017(SELL, PRICE, now=0.0)
    breaker.record_110017(SELL, PRICE, now=1.0)
    assert breaker.record_110017(SELL, PRICE, now=2.0) is True
    assert breaker.is_blocked(SELL, PRICE, now=3.0) is True
    # Different side, same price
    assert breaker.is_blocked("Buy", PRICE, now=3.0) is False
    # Same side, different price
    assert breaker.is_blocked(SELL, Decimal("84.52"), now=3.0) is False


def test_breaker_price_key_normalized_decimal(breaker):
    """Equal Decimals that differ in scale map to the same scope key."""
    breaker.record_110017(SELL, Decimal("84.5"), now=0.0)
    breaker.record_110017(SELL, Decimal("84.50"), now=1.0)
    # Both should land on the same key → 3rd trips
    assert breaker.record_110017(SELL, Decimal("84.500"), now=2.0) is True
