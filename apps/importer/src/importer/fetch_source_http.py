"""Protocol 1.0 HTTP market-data transport."""

from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator, Optional

import requests
from requests.auth import AuthBase

logger = logging.getLogger(__name__)

_TIMEOUT = (5, 30)
_MAX_ATTEMPTS = 5
_BACKOFF_CAP_S = 30.0
_MAX_ERROR_BODY = 1024
_JSON_MEDIA_TYPE = "application/json"
_BULK_FIELDS = (
    "symbol",
    "timestamp",
    "last_price",
    "mark_price",
    "bid1_price",
    "ask1_price",
    "funding_rate",
)
_MARKET_FIELDS = _BULK_FIELDS[2:]


class HttpSourceError(Exception):
    """A terminal transport or protocol failure."""


class _NoOpAuth(AuthBase):
    """Truthy request auth that suppresses session auth and netrc lookup."""

    def __call__(self, request):
        return request


_NO_OP_AUTH = _NoOpAuth()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """Format a naive-UTC datetime as ISO-8601 with a ``Z`` suffix."""
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_z_datetime(value: str) -> datetime:
    """Parse a protocol UTC datetime and normalize it to naive UTC."""
    if len(value) < 12 or value[10] != "T" or not value.endswith("Z"):
        raise ValueError("timestamp must end in Z")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _is_json_content_type(value: str) -> bool:
    return value.split(";", 1)[0].strip().lower() == _JSON_MEDIA_TYPE


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


class HttpSource:
    """Authenticated, cursor-paginated protocol 1.0 client."""

    def __init__(
        self,
        base_url: str,
        batch_size: int = 10000,
        session: Optional[requests.Session] = None,
        api_key: str | None = None,
    ):
        self._base_url = base_url
        self._batch_size = batch_size
        self._api_key = api_key
        self._owns_session = session is None
        self._session = requests.Session() if session is None else session
        self._closed = False

    def close(self) -> None:
        """Close an owned session exactly once; injected sessions remain open."""
        if self._owns_session and not self._closed:
            self._session.close()
            self._closed = True

    def probe_range(self, symbol: str) -> Optional[tuple[datetime, datetime]]:
        """Protocol 1.0 exposes no MIN/MAX endpoint."""
        return None

    def _headers(self) -> dict[str, str | None]:
        authorization = (
            f"Bearer {self._api_key}" if self._api_key is not None else None
        )
        return {"Accept": _JSON_MEDIA_TYPE, "Authorization": authorization}

    def _redact(self, value: object) -> str:
        text = str(value)
        if self._api_key:
            text = text.replace(self._api_key, "[REDACTED]")
        return text

    def _safe_body(self, response) -> str:
        """Read/redact a response body before applying the log-size cap."""
        content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
        if _is_json_content_type(content_type):
            try:
                payload = response.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                detail = payload.get("detail")
                if isinstance(detail, (str, list)):
                    rendered = (
                        detail
                        if isinstance(detail, str)
                        else json.dumps(detail, ensure_ascii=False)
                    )
                    return self._redact(rendered)[:_MAX_ERROR_BODY]
        try:
            body = response.text
        except Exception:
            body = "<unavailable response body>"
        return self._redact(body)[:_MAX_ERROR_BODY]

    def _request_failure(self, endpoint: str, kind: str) -> HttpSourceError:
        return HttpSourceError(f"{endpoint} request failed: {self._redact(kind)}")

    @staticmethod
    def _retry_after_seconds(response, now: datetime) -> float | None:
        raw = str(getattr(response, "headers", {}).get("Retry-After", "")).strip()
        if not raw:
            return None
        if re.fullmatch(r"\d+", raw):
            return float(int(raw))
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = (parsed.astimezone(timezone.utc) - now).total_seconds()
        return seconds if seconds >= 0 else None

    def _request_json(self, endpoint: str, params: dict) -> dict:
        """Run one shared authenticated JSON request with bounded retries."""
        url = f"{self._base_url}/{endpoint}"
        fixed_params = dict(params)
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            retry_kind: str | None = None
            response = None
            try:
                response = self._session.get(
                    url,
                    params=dict(fixed_params),
                    headers=self._headers(),
                    auth=_NO_OP_AUTH,
                    timeout=_TIMEOUT,
                    allow_redirects=False,
                )
            except (requests.exceptions.SSLError, requests.exceptions.ProxyError):
                raise self._request_failure(endpoint, "TLS or proxy configuration") from None
            except (
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ReadTimeout,
            ):
                retry_kind = "timeout"
            except requests.exceptions.ChunkedEncodingError:
                retry_kind = "interrupted response"
            except requests.exceptions.ConnectionError:
                retry_kind = "connection error"
            except (
                requests.exceptions.InvalidURL,
                requests.exceptions.MissingSchema,
                requests.exceptions.InvalidSchema,
                requests.exceptions.InvalidHeader,
            ):
                raise self._request_failure(endpoint, "invalid request configuration") from None
            except requests.RequestException:
                raise self._request_failure(endpoint, "terminal request error") from None
            except Exception as exc:
                safe = self._redact(exc)
                raise self._request_failure(endpoint, f"unexpected request error: {safe}") from None
            else:
                status = response.status_code
                if status == 200:
                    break
                if status == 429 or 500 <= status <= 599:
                    retry_kind = f"HTTP {status}"
                else:
                    body = self._safe_body(response)
                    suffix = f": {body}" if body else ""
                    raise HttpSourceError(
                        f"{endpoint} returned terminal HTTP {status}{suffix}"
                    )

            if attempt == _MAX_ATTEMPTS:
                raise HttpSourceError(
                    f"{endpoint} failed after {_MAX_ATTEMPTS} attempts "
                    f"({retry_kind})"
                )

            ceiling = min(_BACKOFF_CAP_S, 2 ** (attempt - 1))
            delay = random.uniform(0, ceiling)
            if response is not None and response.status_code == 429:
                retry_after = self._retry_after_seconds(response, _utc_now())
                if retry_after is not None:
                    if retry_after > _BACKOFF_CAP_S:
                        raise HttpSourceError(
                            f"{endpoint} Retry-After exceeds the client retry cap"
                        )
                    delay = max(delay, retry_after)
            logger.warning(
                "HTTP %s %s (attempt %d/%d); retrying unchanged request",
                endpoint,
                retry_kind,
                attempt,
                _MAX_ATTEMPTS,
            )
            time.sleep(delay)
        else:  # pragma: no cover - loop either breaks or raises
            raise HttpSourceError(f"{endpoint} request loop exhausted")

        content_type = str(
            getattr(response, "headers", {}).get("Content-Type", "")
        )
        if not _is_json_content_type(content_type):
            raise HttpSourceError(
                f"{endpoint} returned HTTP 200 without application/json"
            )
        try:
            payload = response.json()
        except Exception:
            raise HttpSourceError(f"{endpoint} returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise HttpSourceError(f"{endpoint} JSON must be a top-level object")
        return payload

    @staticmethod
    def _validate_bulk_shape(payload: dict) -> tuple[list, str | None]:
        if "rows" not in payload or "next_cursor" not in payload:
            raise HttpSourceError(
                "ticker_data JSON requires rows and next_cursor"
            )
        rows = payload["rows"]
        cursor = payload["next_cursor"]
        if not isinstance(rows, list):
            raise HttpSourceError("ticker_data rows must be a list")
        if cursor is not None and not isinstance(cursor, str):
            raise HttpSourceError(
                "ticker_data next_cursor must be a string or null"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise HttpSourceError("ticker_data rows must contain objects")
            if any(field not in row for field in _BULK_FIELDS):
                raise HttpSourceError(
                    "ticker_data row is missing a required protocol field"
                )
            if row["symbol"] is not None and not isinstance(row["symbol"], str):
                raise HttpSourceError(
                    "ticker_data row symbol must be a string or null"
                )
            if row["timestamp"] is not None and not isinstance(
                row["timestamp"], str
            ):
                raise HttpSourceError(
                    "ticker_data row timestamp must be a string or null"
                )
            for field in _MARKET_FIELDS:
                value = row[field]
                if value is not None and not _is_finite_number(value):
                    raise HttpSourceError(
                        f"ticker_data row {field} must be a finite number or null"
                    )
        return rows, cursor

    def fetch_batches(
        self, symbol: str, start: datetime, end: datetime
    ) -> Iterator[list[dict]]:
        """Decode complete pages and adopt opaque cursors only after commit."""
        cursor: str | None = None
        adopted: set[str] = set()
        last_timestamp: datetime | None = None
        filtered = {
            "null_symbol": 0,
            "mismatched_symbol": 0,
            "null_timestamp": 0,
            "invalid_timestamp": 0,
        }
        try:
            while True:
                params = {
                    "symbol": symbol,
                    "start": iso_utc(start),
                    "end": iso_utc(end),
                    "limit": self._batch_size,
                }
                if cursor is not None:
                    params["cursor"] = cursor
                payload = self._request_json("ticker_data", params)
                raw_rows, next_cursor = self._validate_bulk_shape(payload)
                if not raw_rows:
                    if next_cursor is not None:
                        raise HttpSourceError(
                            "ticker_data empty rows cannot advance a cursor"
                        )
                    return

                page: list[dict] = []
                page_last = last_timestamp
                for raw in raw_rows:
                    if raw["symbol"] is None:
                        filtered["null_symbol"] += 1
                        continue
                    if raw["symbol"] != symbol:
                        filtered["mismatched_symbol"] += 1
                        continue
                    if raw["timestamp"] is None:
                        filtered["null_timestamp"] += 1
                        continue
                    try:
                        timestamp = _parse_z_datetime(raw["timestamp"])
                    except (ValueError, OverflowError):
                        filtered["invalid_timestamp"] += 1
                        continue
                    if not start <= timestamp <= end:
                        raise HttpSourceError(
                            "ticker_data returned a timestamp outside the "
                            "requested inclusive range"
                        )
                    if page_last is not None and timestamp < page_last:
                        raise HttpSourceError(
                            "ticker_data timestamps decreased; restart into "
                            "a fresh --tag"
                        )
                    page_last = timestamp
                    page.append(
                        {
                            "symbol": symbol,
                            "timestamp": timestamp,
                            **{field: raw[field] for field in _MARKET_FIELDS},
                        }
                    )

                if next_cursor is not None and (
                    next_cursor in adopted or next_cursor == cursor
                ):
                    raise HttpSourceError(
                        "ticker_data repeated or cycled next_cursor"
                    )

                if page:
                    yield page
                    last_timestamp = page_last

                if next_cursor is None:
                    return
                adopted.add(next_cursor)
                cursor = next_cursor
        finally:
            if any(filtered.values()):
                logger.warning(
                    "%s: HTTP damaged-row filters: %s", symbol, filtered
                )

    def fetch_klines(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Fetch and validate a bounded, strictly ordered 1m kline window."""
        if (
            start.second
            or start.microsecond
            or end.second
            or end.microsecond
            or end <= start
            or end - start > timedelta(hours=24)
        ):
            raise HttpSourceError(
                "klines requires a positive, minute-aligned window of at most 24 hours"
            )
        payload = self._request_json(
            "klines",
            {
                "symbol": symbol,
                "interval": "1m",
                "start": iso_utc(start),
                "end": iso_utc(end),
            },
        )
        if "rows" not in payload or not isinstance(payload["rows"], list):
            raise HttpSourceError("klines rows must be present and be a list")

        decoded: list[dict] = []
        previous: datetime | None = None
        for row in payload["rows"]:
            if not isinstance(row, dict):
                raise HttpSourceError("klines rows must contain objects")
            fields = ("start_time", "open", "high", "low", "close")
            if any(field not in row for field in fields):
                raise HttpSourceError("kline row is missing a required field")
            if not isinstance(row["start_time"], str):
                raise HttpSourceError("kline start_time must be a string")
            for field in fields[1:]:
                if not _is_finite_number(row[field]):
                    raise HttpSourceError(
                        f"kline {field} must be a finite non-null number"
                    )
            try:
                start_time = _parse_z_datetime(row["start_time"])
            except (ValueError, OverflowError):
                raise HttpSourceError(
                    "kline start_time must be an ISO UTC datetime ending in Z"
                ) from None
            if start_time.second or start_time.microsecond:
                raise HttpSourceError(
                    "kline start_time must be an exact whole UTC minute"
                )
            if previous is not None and start_time <= previous:
                raise HttpSourceError(
                    "kline start_time values must be strictly increasing"
                )
            if start_time < start or start_time + timedelta(minutes=1) > end:
                raise HttpSourceError(
                    "kline interval lies outside the requested window"
                )
            previous = start_time
            decoded.append(
                {
                    "start_time": start_time,
                    **{field: row[field] for field in fields[1:]},
                }
            )
        return decoded
