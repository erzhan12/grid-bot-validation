"""Tests for the SafetyCaps helper (feature 0079 / issue #182).

SafetyCaps owns all production-safety-cap state and decision logic in one
place. Tested in isolation with an injected fake monotonic clock and an
explicit ``now_utc`` so trip + recovery semantics are deterministic.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from gridbot.config import SafetyCapsConfig
from gridbot.safety_caps import SafetyCaps, CapDecision


class _FakeClock:
    """Deterministic monotonic clock; advance via ``.t``."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _caps(clock=None, **caps_kwargs) -> SafetyCaps:
    return SafetyCaps(
        SafetyCapsConfig(**caps_kwargs),
        strat_id="btcusdt_test",
        clock=clock or _FakeClock(),
    )


_DAY1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_DAY2 = datetime(2026, 1, 2, 0, 30, 0, tzinfo=UTC)


class TestCapDecision:
    def test_is_frozen(self):
        d = CapDecision(allowed=False, reason="max_notional")
        with pytest.raises(Exception):
            d.allowed = True  # frozen dataclass


class TestC1MaxNotional:
    """C1 — max total position notional per symbol; OPEN-only, closes exempt."""

    def test_allows_below_cap(self):
        caps = _caps(max_notional_per_symbol="500")
        assert caps.allow_open(
            total_notional=Decimal("499"), open_order_count=0
        ).allowed is True

    def test_rejects_at_cap(self):
        caps = _caps(max_notional_per_symbol="500")
        d = caps.allow_open(total_notional=Decimal("500"), open_order_count=0)
        assert d.allowed is False
        assert d.reason == "max_notional"

    def test_rejects_above_cap(self):
        caps = _caps(max_notional_per_symbol="500")
        assert caps.allow_open(
            total_notional=Decimal("600"), open_order_count=0
        ).allowed is False

    def test_reduce_only_never_blocked_by_notional(self):
        """C1 must NOT block reduce-only closes (de-risking always proceeds)."""
        caps = _caps(max_notional_per_symbol="500")
        # reduce-only takes no notional arg; far above the C1 cap it still allows.
        assert caps.allow_reduce_only(open_order_count=0).allowed is True

    def test_none_disables_c1(self):
        caps = _caps()  # all None
        assert caps.allow_open(
            total_notional=Decimal("10_000_000"), open_order_count=0
        ).allowed is True


class TestC2MaxOpenOrders:
    """C2 — pure count limit; blocks BOTH open and reduce-only."""

    def test_allows_below_cap(self):
        caps = _caps(max_open_orders=3)
        assert caps.allow_open(
            total_notional=Decimal("0"), open_order_count=2
        ).allowed is True

    def test_rejects_open_at_cap(self):
        caps = _caps(max_open_orders=3)
        d = caps.allow_open(total_notional=Decimal("0"), open_order_count=3)
        assert d.allowed is False
        assert d.reason == "max_open_orders"

    def test_rejects_reduce_only_at_cap(self):
        caps = _caps(max_open_orders=3)
        d = caps.allow_reduce_only(open_order_count=3)
        assert d.allowed is False
        assert d.reason == "max_open_orders"

    def test_auto_recovers_when_count_drops(self):
        caps = _caps(max_open_orders=3)
        assert caps.allow_open(
            total_notional=Decimal("0"), open_order_count=3
        ).allowed is False
        assert caps.allow_open(
            total_notional=Decimal("0"), open_order_count=2
        ).allowed is True


class TestC1C2Precedence:
    def test_notional_checked_before_count(self):
        """When both would trip, C1 (notional) reports first."""
        caps = _caps(max_notional_per_symbol="500", max_open_orders=3)
        d = caps.allow_open(total_notional=Decimal("600"), open_order_count=5)
        assert d.allowed is False
        assert d.reason == "max_notional"


class TestC3LossBreaker:
    """C3 — session realized-loss circuit breaker; latch + recovery."""

    def test_does_not_trip_above_limit(self):
        caps = _caps(session_loss_limit="25")
        assert caps.check_loss_breaker(
            session_realized_pnl=Decimal("-10"), now_utc=_DAY1
        ) is False
        assert caps.loss_tripped() is False

    def test_trips_once_at_limit(self):
        caps = _caps(session_loss_limit="25")
        assert caps.check_loss_breaker(
            session_realized_pnl=Decimal("-25"), now_utc=_DAY1
        ) is True
        assert caps.loss_tripped() is True

    def test_latch_idempotent(self):
        caps = _caps(session_loss_limit="25")
        assert caps.check_loss_breaker(
            session_realized_pnl=Decimal("-25"), now_utc=_DAY1
        ) is True
        # Already tripped → subsequent worse losses are not "newly tripped".
        assert caps.check_loss_breaker(
            session_realized_pnl=Decimal("-40"), now_utc=_DAY1
        ) is False
        assert caps.loss_tripped() is True

    def test_recovery_utc_midnight(self):
        caps = _caps(session_loss_limit="25",
                     session_loss_auto_reset_utc_midnight=True)
        caps.check_loss_breaker(session_realized_pnl=Decimal("-25"), now_utc=_DAY1)
        assert caps.loss_tripped() is True
        # Next UTC date with recovered PnL clears the latch.
        assert caps.check_loss_breaker(
            session_realized_pnl=Decimal("0"), now_utc=_DAY2
        ) is False
        assert caps.loss_tripped() is False

    def test_can_retrip_on_new_day(self):
        caps = _caps(session_loss_limit="25",
                     session_loss_auto_reset_utc_midnight=True)
        caps.check_loss_breaker(session_realized_pnl=Decimal("-25"), now_utc=_DAY1)
        # New day, still in loss → resets then trips again.
        assert caps.check_loss_breaker(
            session_realized_pnl=Decimal("-25"), now_utc=_DAY2
        ) is True
        assert caps.loss_tripped() is True

    def test_recovery_manual_only(self):
        caps = _caps(session_loss_limit="25",
                     session_loss_auto_reset_utc_midnight=False)
        caps.check_loss_breaker(session_realized_pnl=Decimal("-25"), now_utc=_DAY1)
        assert caps.loss_tripped() is True
        # New UTC date does NOT clear when auto-reset is off.
        caps.check_loss_breaker(session_realized_pnl=Decimal("0"), now_utc=_DAY2)
        assert caps.loss_tripped() is True

    def test_none_disables_c3(self):
        caps = _caps()  # session_loss_limit None
        assert caps.check_loss_breaker(
            session_realized_pnl=Decimal("-99999"), now_utc=_DAY1
        ) is False
        assert caps.loss_tripped() is False


class TestC4RateLimit:
    """C4 — max accepted real submissions per trailing 60s."""

    def test_not_limited_below_cap(self):
        clock = _FakeClock(1000.0)
        caps = _caps(clock=clock, max_orders_per_minute=3)
        caps.record_accepted_submission(clock())
        caps.record_accepted_submission(clock())
        assert caps.rate_limited(clock()) is False

    def test_limited_at_cap_within_window(self):
        clock = _FakeClock(1000.0)
        caps = _caps(clock=clock, max_orders_per_minute=3)
        for _ in range(3):
            caps.record_accepted_submission(clock())
        # Same instant — all three inside the 60s window.
        assert caps.rate_limited(clock()) is True
        # 30s later still within window.
        assert caps.rate_limited(1030.0) is True

    def test_recovery_after_window_decay(self):
        clock = _FakeClock(1000.0)
        caps = _caps(clock=clock, max_orders_per_minute=3)
        for _ in range(3):
            caps.record_accepted_submission(clock())
        # Advance > 60s past every recorded submission → window evicts.
        assert caps.rate_limited(1061.0) is False

    def test_none_disables_c4(self):
        clock = _FakeClock(1000.0)
        caps = _caps(clock=clock)  # max_orders_per_minute None
        for _ in range(100):
            caps.record_accepted_submission(clock())
        assert caps.rate_limited(clock()) is False


class TestMasterKillSwitch:
    """enabled=False → every cap inert regardless of per-cap values."""

    def test_all_caps_inert_when_disabled(self):
        clock = _FakeClock(1000.0)
        caps = SafetyCaps(
            SafetyCapsConfig(
                enabled=False,
                max_notional_per_symbol="1",
                max_open_orders=1,
                session_loss_limit="1",
                max_orders_per_minute=1,
            ),
            strat_id="btcusdt_test",
            clock=clock,
        )
        assert caps.allow_open(
            total_notional=Decimal("10_000"), open_order_count=99
        ).allowed is True
        assert caps.allow_reduce_only(open_order_count=99).allowed is True
        assert caps.check_loss_breaker(
            session_realized_pnl=Decimal("-10_000"), now_utc=_DAY1
        ) is False
        assert caps.loss_tripped() is False
        for _ in range(10):
            caps.record_accepted_submission(clock())
        assert caps.rate_limited(clock()) is False
