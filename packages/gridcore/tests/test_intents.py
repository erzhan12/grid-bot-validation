"""Tests for gridcore.intents module."""

import pytest

from gridcore.intents import extract_client_order_prefix


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
