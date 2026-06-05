"""Feature 0066 (issue #159) — moderate_liq_risk low-balance bug-fix.

Under exhausted balance the moderate_liq_risk arm must NOT throttle the
opposite side's close orders (the 0.5 hedge-throttle), because those closes
free the margin that re-enables opens — the throttle caused the storm deadlock.
When low_balance is False (the default, and the killswitch-off path) behavior
is byte-for-byte the pre-0066 hedge-throttle.
"""

from gridcore.position import Position, RiskConfig


def _pair():
    rc = RiskConfig(
        min_liq_ratio=0.8, max_liq_ratio=1.2, max_margin=8.0, min_total_margin=0.15,
    )
    long_p, short_p = Position.create_linked_pair(rc)
    long_p.reset_amount_multiplier()
    short_p.reset_amount_multiplier()
    return long_p, short_p


# Long-branch moderate_liq_risk fires for min_liq_ratio < liq_ratio <= 1.05*min.
# (0.8, 0.84] → use 0.82. It throttles the opposite SHORT's Buy (close-short).

def test_long_moderate_liq_throttles_close_short_normally():
    long_p, short_p = _pair()
    long_p._apply_long_position_rules(0.82, False, 1.0, -1.0, low_balance=False)
    assert short_p.get_amount_multiplier()["Buy"] == 0.5


def test_long_moderate_liq_skips_close_throttle_under_low_balance():
    long_p, short_p = _pair()
    long_p._apply_long_position_rules(0.82, False, 1.0, -1.0, low_balance=True)
    assert short_p.get_amount_multiplier()["Buy"] == 1.0  # NOT throttled


# Short-branch moderate_liq_risk fires for 0.95*max <= liq_ratio < max.
# [1.14, 1.2) → use 1.15. It throttles the opposite LONG's Sell (close-long).

def test_short_moderate_liq_throttles_close_long_normally():
    long_p, short_p = _pair()
    short_p._apply_short_position_rules(1.15, False, 1.0, -1.0, low_balance=False)
    assert long_p.get_amount_multiplier()["Sell"] == 0.5


def test_short_moderate_liq_skips_close_throttle_under_low_balance():
    long_p, short_p = _pair()
    short_p._apply_short_position_rules(1.15, False, 1.0, -1.0, low_balance=True)
    assert long_p.get_amount_multiplier()["Sell"] == 1.0  # NOT throttled


def test_low_balance_default_is_false_preserves_behavior():
    """Omitting low_balance (old callers / backtest) keeps the throttle."""
    long_p, short_p = _pair()
    long_p._apply_long_position_rules(0.82, False, 1.0, -1.0)
    assert short_p.get_amount_multiplier()["Buy"] == 0.5
