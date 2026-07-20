"""Tests for transport B (HTTP cursor pagination, retry/backoff) — feature 0093."""

from datetime import datetime

import pytest
import requests

from importer.fetch_source_http import HttpSource, HttpSourceError

_T0 = datetime(2026, 7, 1, 0, 0, 0)
_T1 = datetime(2026, 7, 1, 1, 0, 0)


def _http_row(row_id: int, ts_iso: str, price: float = 100.0) -> dict:
    return {
        "id": row_id,
        "symbol": "BTCUSDT",
        "timestamp": ts_iso,
        "last_price": price,
        "mark_price": price,
        "bid1_price": price,
        "ask1_price": price,
        "funding_rate": 0.0001,
    }


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Scripted requests.Session stand-in; records every call's params."""

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Backoff sleeps are a no-op in tests."""
    monkeypatch.setattr("importer.fetch_source_http.time.sleep", lambda s: None)


class TestHttpSource:
    def test_pagination_follows_next_cursor(self):
        """Batches follow next_cursor to exhaustion."""
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "rows": [_http_row(1, "2026-07-01T00:00:00Z")],
                        "next_cursor": "c1",
                    },
                ),
                FakeResponse(
                    200,
                    {
                        "rows": [_http_row(2, "2026-07-01T00:00:01Z")],
                        "next_cursor": None,
                    },
                ),
            ]
        )
        source = HttpSource("http://src", session=session)
        batches = list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert [r["id"] for b in batches for r in b] == [1, 2]
        assert "cursor" not in session.calls[0]
        assert session.calls[1]["cursor"] == "c1"

    def test_timestamps_yielded_naive_utc(self):
        """ISO-8601 Z timestamps are converted to naive UTC at the boundary."""
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "rows": [_http_row(1, "2026-07-01T00:00:00Z")],
                        "next_cursor": None,
                    },
                )
            ]
        )
        source = HttpSource("http://src", session=session)
        (batch,) = list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert batch[0]["timestamp"] == _T0
        assert batch[0]["timestamp"].tzinfo is None

    def test_retry_on_429_re_requests_same_cursor(self):
        """429 retries re-request the identical cursor (no skipped rows)."""
        ok = FakeResponse(
            200,
            {"rows": [_http_row(1, "2026-07-01T00:00:00Z")], "next_cursor": None},
        )
        session = FakeSession([FakeResponse(429), ok])
        source = HttpSource("http://src", session=session)
        batches = list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert len(batches) == 1
        assert session.calls[0] == session.calls[1]

    def test_retry_on_5xx_and_connection_error(self):
        """503 and connection errors are retried with the same params."""
        ok = FakeResponse(200, {"rows": [], "next_cursor": None})
        session = FakeSession(
            [FakeResponse(503), requests.ConnectionError("boom"), ok]
        )
        source = HttpSource("http://src", session=session)
        assert list(source.fetch_batches("BTCUSDT", _T0, _T1)) == []
        assert session.calls[0] == session.calls[1] == session.calls[2]

    def test_non_retryable_4xx_aborts(self):
        """A 404 aborts immediately with the body logged."""
        session = FakeSession([FakeResponse(404, text="not found")])
        source = HttpSource("http://src", session=session)
        with pytest.raises(HttpSourceError, match="non-retryable HTTP 404"):
            list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert len(session.calls) == 1

    def test_exhausted_retries_abort(self):
        """Five consecutive retryable failures give up with an error."""
        session = FakeSession([FakeResponse(500)] * 5)
        source = HttpSource("http://src", session=session)
        with pytest.raises(HttpSourceError, match="after 5 attempts"):
            list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert len(session.calls) == 5

    def test_empty_rows_with_truthy_cursor_terminates(self):
        """An empty rows page ends pagination even with a (buggy) cursor."""
        session = FakeSession(
            [FakeResponse(200, {"rows": [], "next_cursor": "c-loop"})]
        )
        source = HttpSource("http://src", session=session)
        assert list(source.fetch_batches("BTCUSDT", _T0, _T1)) == []
        assert len(session.calls) == 1

    def test_empty_string_cursor_terminates(self):
        """A falsy (empty-string) next_cursor ends pagination instead of spinning."""
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "rows": [_http_row(1, "2026-07-01T00:00:00Z")],
                        "next_cursor": "",
                    },
                )
            ]
        )
        source = HttpSource("http://src", session=session)
        batches = list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert len(batches) == 1
        assert len(session.calls) == 1

    def test_probe_range_unsupported(self):
        """The HTTP contract has no MIN/MAX probe."""
        assert HttpSource("http://src", session=FakeSession([])).probe_range(
            "BTCUSDT"
        ) is None

    def test_request_window_is_iso_z(self):
        """start/end are sent as ISO-8601 UTC with Z suffix."""
        session = FakeSession([FakeResponse(200, {"rows": [], "next_cursor": None})])
        source = HttpSource("http://src", session=session)
        list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert session.calls[0]["start"] == "2026-07-01T00:00:00Z"
        assert session.calls[0]["end"] == "2026-07-01T01:00:00Z"
