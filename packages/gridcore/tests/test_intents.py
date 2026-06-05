"""Tests for gridcore.intents module."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from gridcore.intents import PlaceLimitIntent, extract_client_order_prefix


@pytest.mark.parametrize("order_link_id, expected", [
    (None, None),
    ("", None),
    ("abc123", "abc123"),                        # pre-hotfix, no suffix
    ("abc123-1715170800000", "abc123"),          # post-hotfix 2026-05-08
    ("-foo", None),                              # leading hyphen → empty prefix → None
    ("foo-", "foo"),                             # trailing hyphen → empty suffix
    ("a-b-c", "a"),                              # multiple hyphens → split on first
    ("0123456789abcdef-1715170800000", "0123456789abcdef"),  # full 16-hex prefix
])
def test_extract_client_order_prefix(order_link_id, expected):
    assert extract_client_order_prefix(order_link_id) == expected


def _make_place_intent() -> PlaceLimitIntent:
    return PlaceLimitIntent.create(
        symbol="BTCUSDT",
        side="Buy",
        price=Decimal("50000.0"),
        qty=Decimal("0.001"),
        grid_level=10,
        direction="long",
    )


def test_place_intent_order_link_id_defaults_to_none_and_is_frozen():
    intent = _make_place_intent()

    assert intent.order_link_id is None
    with pytest.raises(FrozenInstanceError):
        intent.order_link_id = f"{intent.client_order_id}-1715170800000"


def test_place_intent_order_link_id_replace_preserves_original():
    intent = _make_place_intent()
    assigned = replace(
        intent,
        order_link_id=f"{intent.client_order_id}-1715170800000",
    )

    assert intent.order_link_id is None
    assert assigned is not intent
    assert assigned.order_link_id == f"{intent.client_order_id}-1715170800000"
    assert extract_client_order_prefix(assigned.order_link_id) == intent.client_order_id


def test_place_intent_order_link_id_does_not_affect_equality_or_hash():
    intent = _make_place_intent()
    first = replace(intent, order_link_id=f"{intent.client_order_id}-1")
    second = replace(intent, order_link_id=f"{intent.client_order_id}-2")

    assert first == second
    assert hash(first) == hash(second)


def test_place_intent_order_link_id_not_in_identity_params():
    intent = _make_place_intent()
    assigned = replace(intent, order_link_id=f"{intent.client_order_id}-1715170800000")

    assert "order_link_id" not in PlaceLimitIntent._IDENTITY_PARAMS
    assert assigned.client_order_id == intent.client_order_id


def test_post_only_default_false_preserves_identity():
    """Feature 0066: post_only is excluded from identity (id + ==/hash)."""
    plain = PlaceLimitIntent.create(
        symbol="BTCUSDT", side="Buy", price=Decimal("100"), qty=Decimal("0.1"),
        grid_level=3, direction="long",
    )
    maker = PlaceLimitIntent.create(
        symbol="BTCUSDT", side="Buy", price=Decimal("100"), qty=Decimal("0.1"),
        grid_level=3, direction="long", post_only=True,
    )
    assert plain.post_only is False
    assert maker.post_only is True
    assert maker.client_order_id == plain.client_order_id  # id unaffected
    assert maker == plain                                   # compare=False
    assert hash(maker) == hash(plain)
    assert "post_only" not in PlaceLimitIntent._IDENTITY_PARAMS
