"""Pure source-row -> TickerSnapshot mapping with NULL fallbacks (feature 0093).

Hermetic, no I/O. Every numeric crosses the boundary as
``Decimal(str(x))`` quantized to 8dp — never a raw float bind (project
precision rule). All target price columns are ``nullable=False``, so the
NULL fallbacks are mandatory to satisfy the schema.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Optional

from grid_db.models import TickerSnapshot

_EIGHT_DP = Decimal("0.00000001")


@dataclass
class FallbackCounters:
    """Per-field NULL-fallback / skip counters, logged after import."""

    skipped_null_last_price: int = 0
    mark_price_fallback: int = 0
    bid1_price_fallback: int = 0
    ask1_price_fallback: int = 0
    funding_rate_fallback: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _to_decimal(value: float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(_EIGHT_DP)


def map_row(row: dict, counters: FallbackCounters) -> Optional[TickerSnapshot]:
    """Map one source row dict to a TickerSnapshot ORM instance.

    Returns None (and counts the skip) when ``last_price`` is NULL — there
    is no fallback source and the target column is ``nullable=False``.
    NULL mark/bid1/ask1 fall back to ``last_price``; NULL funding_rate
    falls back to 0. Each fallback increments its counter.
    """
    last = row.get("last_price")
    if last is None:
        counters.skipped_null_last_price += 1
        return None
    last_price = _to_decimal(last)

    mark = row.get("mark_price")
    if mark is None:
        counters.mark_price_fallback += 1
        mark_price = last_price
    else:
        mark_price = _to_decimal(mark)

    bid1 = row.get("bid1_price")
    if bid1 is None:
        counters.bid1_price_fallback += 1
        bid1_price = last_price
    else:
        bid1_price = _to_decimal(bid1)

    ask1 = row.get("ask1_price")
    if ask1 is None:
        counters.ask1_price_fallback += 1
        ask1_price = last_price
    else:
        ask1_price = _to_decimal(ask1)

    funding = row.get("funding_rate")
    if funding is None:
        counters.funding_rate_fallback += 1
        funding_rate = Decimal("0")
    else:
        funding_rate = _to_decimal(funding)

    return TickerSnapshot(
        symbol=row["symbol"],
        exchange_ts=row["timestamp"],
        # No recv-ts on the source; local_ts mirrors exchange_ts.
        local_ts=row["timestamp"],
        last_price=last_price,
        mark_price=mark_price,
        bid1_price=bid1_price,
        ask1_price=ask1_price,
        funding_rate=funding_rate,
        raw_json=None,
    )
