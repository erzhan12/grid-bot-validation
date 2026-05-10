"""Helpers for Bybit orderLinkId wire identifiers."""

from datetime import UTC, datetime
from typing import Callable


def _now_utc_ms() -> int:
    """Current UTC epoch time in milliseconds."""
    return int(datetime.now(UTC).timestamp() * 1000)


def make_order_link_id(
    client_order_id: str,
    *,
    now_ms: Callable[[], int] = _now_utc_ms,
) -> str:
    """Build the wire-form orderLinkId for one placement lifecycle."""
    return f"{client_order_id}-{now_ms()}"
