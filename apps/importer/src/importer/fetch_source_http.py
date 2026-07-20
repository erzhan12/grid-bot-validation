"""Transport B: HTTP API client for trad_save_history (feature 0093).

Client side of the plan's HTTP contract:
``GET {base_url}/ticker_data?symbol&start&end&limit&cursor`` returning
``{"rows": [...], "next_cursor": <opaque or null>}`` in stable
``(timestamp, id)`` ascending order, both bounds inclusive.

Robustness for long multi-hour pulls: 30 s per-request timeout; retry on
connection errors / HTTP 429 / 5xx with exponential backoff re-requesting
the SAME cursor (idempotent GET, so a retried page cannot skip rows); any
other 4xx aborts with the response body logged.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

import requests

from importer.config import to_naive_utc

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30
_MAX_ATTEMPTS = 5
_BACKOFF_BASE_S = 1.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_CAP_S = 30.0


class HttpSourceError(Exception):
    """Non-recoverable HTTP source failure (aborts the import)."""


def iso_utc(dt: datetime) -> str:
    """Format a naive-UTC datetime as ISO-8601 with the ``Z`` suffix."""
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


class HttpSource:
    """Cursor-paginated reader against the trad_save_history HTTP API."""

    def __init__(
        self,
        base_url: str,
        batch_size: int = 10000,
        session: Optional[requests.Session] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._batch_size = batch_size
        self._session = session or requests.Session()

    def fetch_batches(
        self, symbol: str, start: datetime, end: datetime
    ) -> Iterator[list[dict]]:
        """Yield batches following ``next_cursor`` to exhaustion."""
        cursor: Optional[str] = None
        while True:
            params: dict = {
                "symbol": symbol,
                "start": iso_utc(start),
                "end": iso_utc(end),
                "limit": self._batch_size,
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = self._get_with_retry(params)
            rows = payload.get("rows") or []
            cursor = payload.get("next_cursor")
            if not rows:
                # A page with no rows cannot make progress — treat as
                # exhausted even if the server sent a cursor (a buggy
                # truthy cursor would otherwise spin forever).
                if cursor:
                    logger.warning(
                        "HTTP source sent next_cursor with an empty rows "
                        "page — treating as exhausted"
                    )
                return
            yield [self._normalize_row(r) for r in rows]
            # Contract says null when exhausted; treat any falsy cursor
            # (e.g. "") as exhausted too — re-sending it would spin forever.
            if not cursor:
                return

    def probe_range(self, symbol: str) -> Optional[tuple[datetime, datetime]]:
        """The HTTP contract exposes no MIN/MAX probe — always None.

        The caller requires explicit ``--start``/``--end`` for this
        transport.
        """
        return None

    def _get_with_retry(self, params: dict) -> dict:
        """GET with exponential backoff; retried pages re-request the same cursor."""
        url = f"{self._base_url}/ticker_data"
        delay = _BACKOFF_BASE_S
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            failure: str
            try:
                response = self._session.get(url, params=params, timeout=_TIMEOUT_S)
            except requests.RequestException as e:
                failure = f"connection error: {e}"
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 429 or response.status_code >= 500:
                    failure = f"HTTP {response.status_code}"
                else:
                    logger.error(
                        "HTTP source returned %d: %s",
                        response.status_code,
                        response.text,
                    )
                    raise HttpSourceError(
                        f"non-retryable HTTP {response.status_code} from {url}"
                    )
            if attempt == _MAX_ATTEMPTS:
                raise HttpSourceError(
                    f"{failure} after {_MAX_ATTEMPTS} attempts against {url}"
                )
            logger.warning(
                "HTTP source %s (attempt %d/%d), retrying same cursor in %.0fs",
                failure,
                attempt,
                _MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * _BACKOFF_FACTOR, _BACKOFF_CAP_S)
        raise HttpSourceError("unreachable")  # pragma: no cover

    @staticmethod
    def _normalize_row(row: dict) -> dict:
        """Parse the ISO-8601 timestamp to naive UTC at the transport boundary."""
        out = dict(row)
        ts = out["timestamp"]
        if isinstance(ts, str):
            normalized = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
            ts = datetime.fromisoformat(normalized)
        out["timestamp"] = to_naive_utc(ts)
        return out
