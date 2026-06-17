"""Helpers for Bybit orderLinkId wire identifiers."""

from datetime import UTC, datetime
from typing import Callable

# Bybit V5 caps orderLinkId at 36 characters. The wire form is
# f"{client_order_id}-{millis}" = 16 + 1 + 13 = 30 chars; feature 0080 (issue
# #183) namespaces the hash INPUT only (not the wire form), so length is
# unchanged. Guard against future format drift — an over-length id is a
# guaranteed exchange reject, so fail fast at construction.
_BYBIT_ORDER_LINK_ID_MAX = 36


def _now_utc_ms() -> int:
    """Current UTC epoch time in milliseconds."""
    return int(datetime.now(UTC).timestamp() * 1000)


def make_order_link_id(
    client_order_id: str,
    *,
    now_ms: Callable[[], int] = _now_utc_ms,
) -> str:
    """Build the wire-form orderLinkId for one placement lifecycle."""
    wire_id = f"{client_order_id}-{now_ms()}"
    if len(wire_id) > _BYBIT_ORDER_LINK_ID_MAX:
        raise ValueError(
            f"orderLinkId {wire_id!r} exceeds Bybit's "
            f"{_BYBIT_ORDER_LINK_ID_MAX}-char limit ({len(wire_id)} chars)"
        )
    return wire_id
