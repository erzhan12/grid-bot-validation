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


# --- Feature 0080 (issue #183): strat_id namespacing of the identity hash ---

# Pinned reference: pre-0080 hash of ("BTCUSDT","Buy","50000.0","long") with no
# strat_id. Guards the None-default back-compat (existing callers + historical
# recorded rows) against any formula drift.
_OLD_HASH_NO_STRAT = "bedf732012a9abc1"


def test_strat_id_none_default_preserves_old_hash():
    """strat_id=None reproduces the pre-0080 client_order_id byte-for-byte."""
    plain = _make_place_intent()  # no strat_id passed
    explicit_none = PlaceLimitIntent.create(
        symbol="BTCUSDT", side="Buy", price=Decimal("50000.0"), qty=Decimal("0.001"),
        grid_level=10, direction="long", strat_id=None,
    )
    assert plain.client_order_id == _OLD_HASH_NO_STRAT
    assert explicit_none.client_order_id == _OLD_HASH_NO_STRAT


def test_strat_id_namespaces_client_order_id():
    """Same (symbol,side,price,direction), different strat_id -> DIFFERENT id.

    The #183 fix: two strategies (e.g. on different accounts) no longer collide
    on the deterministic prefix.
    """
    base = dict(symbol="BTCUSDT", side="Buy", price=Decimal("50000.0"),
                qty=Decimal("0.001"), grid_level=10, direction="long")
    s1 = PlaceLimitIntent.create(**base, strat_id="s1")
    s2 = PlaceLimitIntent.create(**base, strat_id="s2")
    none = PlaceLimitIntent.create(**base)
    assert s1.client_order_id != s2.client_order_id
    assert s1.client_order_id != none.client_order_id
    assert s2.client_order_id != none.client_order_id


def test_strat_id_stable_within_a_strat():
    """Same (strat_id,symbol,side,price,direction) -> SAME id (deterministic; #110)."""
    base = dict(symbol="BTCUSDT", side="Buy", price=Decimal("50000.0"),
                qty=Decimal("0.001"), grid_level=10, direction="long")
    first = PlaceLimitIntent.create(**base, strat_id="s1")
    second = PlaceLimitIntent.create(**base, strat_id="s1")
    assert first.client_order_id == second.client_order_id


def test_strat_id_does_not_change_identity_param_set():
    """strat_id is a salt, not an identity field; grid_level still excluded."""
    base = dict(symbol="BTCUSDT", side="Buy", price=Decimal("50000.0"),
                qty=Decimal("0.001"), direction="long")
    lvl1 = PlaceLimitIntent.create(**base, grid_level=1, strat_id="s1")
    lvl2 = PlaceLimitIntent.create(**base, grid_level=99, strat_id="s1")
    assert lvl1.client_order_id == lvl2.client_order_id  # grid_level still excluded
    assert "strat_id" not in PlaceLimitIntent._IDENTITY_PARAMS


def test_strat_id_cross_strat_boundary_match():
    """Replay must salt with the LIVE strat_id to match recorded orders.

    A live order recorded under strat_id='ltcusdt_test' yields a wire prefix
    that a replay-side create(strat_id='ltcusdt_test') reproduces, but a
    synthetic replay strat_id ('replay_ltcusdt') does NOT. This is why
    replay/engine.py derives strat_id from seed.strat_id (feature 0080) — the
    comparator joins live vs backtest on client_order_id.
    """
    live = PlaceLimitIntent.create(
        symbol="LTCUSDT", side="Buy", price=Decimal("44.0"), qty=Decimal("0.2"),
        grid_level=5, direction="long", strat_id="ltcusdt_test",
    )
    recorded_wire = f"{live.client_order_id}-1715170800000"

    replay_live = PlaceLimitIntent.create(
        symbol="LTCUSDT", side="Buy", price=Decimal("44.0"), qty=Decimal("0.2"),
        grid_level=5, direction="long", strat_id="ltcusdt_test",
    )
    replay_synthetic = PlaceLimitIntent.create(
        symbol="LTCUSDT", side="Buy", price=Decimal("44.0"), qty=Decimal("0.2"),
        grid_level=5, direction="long", strat_id="replay_ltcusdt",
    )

    # Positive: replay salted with the live strat_id matches the recorded prefix
    # (snapshot-loader round-trip via extract_client_order_prefix).
    assert extract_client_order_prefix(recorded_wire) == replay_live.client_order_id
    # Negative: a synthetic replay strat_id would NOT match (documents engine.py:337).
    assert extract_client_order_prefix(recorded_wire) != replay_synthetic.client_order_id
