"""Source transport protocols and factory."""

from __future__ import annotations

from datetime import datetime
from typing import Iterator, Optional, Protocol


class SourceTransport(Protocol):
    """Read-only access to the trad_save_history ``ticker_data`` stream.

    ``fetch_batches`` yields bounded row dicts containing ``symbol``,
    ``timestamp``, and the five market fields. HTTP rows need no ``id``;
    market values are nullable and unknown response fields are ignored.
    Row ``timestamp`` values are already naive UTC. ``start``/``end`` are
    inclusive.
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


class HttpKlineSource(Protocol):
    """Narrow, already-validated HTTP kline contract used by validation."""

    def fetch_klines(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Return ordered 1m rows with naive-UTC ``start_time`` values."""
        ...


def make_source(
    kind: str,
    url: str,
    batch_size: int = 10000,
    api_key: str | None = None,
) -> SourceTransport:
    """Construct the transport named by ``--source``."""
    if kind == "db":
        from importer.fetch_source_db import DbSource

        return DbSource(url, batch_size=batch_size)
    if kind == "http":
        from importer.fetch_source_http import HttpSource

        return HttpSource(url, batch_size=batch_size, api_key=api_key)
    raise ValueError(f"unknown source kind: {kind!r}")
