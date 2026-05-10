"""Tests for gridbot.order_link_id helpers."""

from gridcore.intents import extract_client_order_prefix
from gridbot.order_link_id import make_order_link_id


def test_make_order_link_id_uses_prefix_and_millis():
    link_id = make_order_link_id("abc123", now_ms=lambda: 1715170800000)

    assert link_id == "abc123-1715170800000"


def test_make_order_link_id_round_trips_to_prefix():
    prefix = "0123456789abcdef"
    link_id = make_order_link_id(prefix, now_ms=lambda: 1715170800000)

    assert extract_client_order_prefix(link_id) == prefix
