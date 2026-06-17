"""Tests for gridbot.order_link_id helpers."""

import pytest

from gridcore.intents import extract_client_order_prefix
from gridbot.order_link_id import _BYBIT_ORDER_LINK_ID_MAX, make_order_link_id


def test_make_order_link_id_uses_prefix_and_millis():
    link_id = make_order_link_id("abc123", now_ms=lambda: 1715170800000)

    assert link_id == "abc123-1715170800000"


def test_make_order_link_id_round_trips_to_prefix():
    prefix = "0123456789abcdef"
    link_id = make_order_link_id(prefix, now_ms=lambda: 1715170800000)

    assert extract_client_order_prefix(link_id) == prefix


def test_make_order_link_id_within_bybit_limit():
    """Feature 0080: a 16-hex prefix + millis is 30 chars, under the 36 limit."""
    link_id = make_order_link_id("0123456789abcdef", now_ms=lambda: 1715170800000)

    assert len(link_id) <= _BYBIT_ORDER_LINK_ID_MAX


def test_make_order_link_id_rejects_overlength():
    """Feature 0080: an over-length wire id raises (guaranteed exchange reject)."""
    with pytest.raises(ValueError, match="36-char limit"):
        make_order_link_id("x" * 30, now_ms=lambda: 1715170800000)
