"""Source transport protocol + factory (feature 0093)."""

from __future__ import annotations

from datetime import datetime
from typing import Iterator, Optional, Protocol


class SourceTransport(Protocol):
    """Read-only access to the trad_save_history ``ticker_data`` stream.

    ``fetch_batches`` yields bounded batches of row dicts with keys
    ``id, symbol, timestamp, last_price, mark_price, bid1_price,
    ask1_price, funding_rate``. Row ``timestamp`` values are ALREADY naive
    UTC — normalization happens at the transport boundary because the
    resume skip compares them against a naive SQLite cursor BEFORE the
    mapping layer runs. ``start``/``end`` bounds are both inclusive
    (replay's window filter is inclusive, and the default ``--end``
    resolves to the source MAX probe).
    """

    def fetch_batches(
        self, symbol: str, start: datetime, end: datetime
    ) -> Iterator[list[dict]]:
        """Yield bounded batches of source rows ordered by (timestamp, id)."""
        ...

    def probe_range(self, symbol: str) -> Optional[tuple[datetime, datetime]]:
        """(MIN, MAX) source timestamp for a symbol.

        Returns None when the source is empty for the symbol, or when the
        transport cannot probe (HTTP contract has no MIN/MAX endpoint —
        the caller must then require explicit ``--start``/``--end``).
        """
        ...


def make_source(kind: str, url: str, batch_size: int = 10000) -> SourceTransport:
    """Construct the transport named by ``--source``."""
    if kind == "db":
        from importer.fetch_source_db import DbSource

        return DbSource(url, batch_size=batch_size)
    if kind == "http":
        from importer.fetch_source_http import HttpSource

        return HttpSource(url, batch_size=batch_size)
    raise ValueError(f"unknown source kind: {kind!r}")
