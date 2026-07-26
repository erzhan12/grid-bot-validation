"""Protocol 1.0 HTTP transport tests."""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from importer.fetch_source_http import HttpSource, HttpSourceError

_T0 = datetime(2026, 7, 1, 0, 0)
_T1 = datetime(2026, 7, 1, 1, 0)


def _row(ts="2026-07-01T00:00:00Z", symbol="BTCUSDT", **overrides):
    row = {
        "symbol": symbol,
        "timestamp": ts,
        "last_price": 100.0,
        "mark_price": 100,
        "bid1_price": 99.5,
        "ask1_price": 100.5,
        "funding_rate": None,
    }
    row.update(overrides)
    return row


def _response(
    status=200,
    payload=None,
    *,
    content_type="application/json; charset=utf-8",
    headers=None,
    raw=None,
):
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    response.headers.update(headers or {})
    response._content = (
        raw
        if raw is not None
        else json.dumps(payload if payload is not None else {}).encode()
    )
    return response


class ScriptedSession(requests.Session):
    """Real request preparation plus scripted, network-free responses."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.sent = []
        self.close_calls = 0

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        item.request = request
        item.url = request.url
        return item

    def close(self):
        self.close_calls += 1
        super().close()


@pytest.fixture(autouse=True)
def deterministic_retry(monkeypatch):
    monkeypatch.setattr("importer.fetch_source_http.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "importer.fetch_source_http.random.uniform", lambda low, high: high
    )


def _source(script, **kwargs):
    session = ScriptedSession(script)
    return HttpSource("https://example.test/deploy", session=session, **kwargs), session


class TestRequests:
    def test_auth_headers_timeout_redirects_and_base_path(self):
        source, session = _source(
            [_response(payload={"rows": [], "next_cursor": None})],
            api_key="opaque-token==",
        )
        assert list(source.fetch_batches("BtC/USDT", _T0, _T1)) == []
        request, kwargs = session.sent[0]
        assert request.url.startswith(
            "https://example.test/deploy/ticker_data?"
        )
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Authorization"] == "Bearer opaque-token=="
        assert kwargs["timeout"] == (5, 30)
        assert kwargs["allow_redirects"] is False
        query = parse_qs(urlsplit(request.url).query)
        assert query["symbol"] == ["BtC/USDT"]
        assert query["start"] == ["2026-07-01T00:00:00Z"]

    def test_no_key_removes_inherited_header_and_session_auth(self):
        source, session = _source(
            [_response(payload={"rows": [], "next_cursor": None})]
        )
        session.headers["Authorization"] = "Bearer inherited"
        session.auth = ("netrc-user", "netrc-password")
        original_auth = session.auth
        list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert "Authorization" not in session.sent[0][0].headers
        assert session.headers["Authorization"] == "Bearer inherited"
        assert session.auth == original_auth

    def test_explicit_key_wins_without_mutating_injected_session(self):
        source, session = _source(
            [_response(payload={"rows": [], "next_cursor": None})],
            api_key="new-token",
        )
        session.headers["Authorization"] = "Bearer old-token"
        session.auth = ("user", "password")
        list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert session.sent[0][0].headers["Authorization"] == "Bearer new-token"
        assert session.headers["Authorization"] == "Bearer old-token"
        assert session.auth == ("user", "password")

    def test_truthy_request_auth_suppresses_netrc(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            requests.sessions,
            "get_netrc_auth",
            lambda url: called.append(url) or ("user", "password"),
        )
        source, session = _source(
            [_response(payload={"rows": [], "next_cursor": None})]
        )
        list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert called == []
        assert "Authorization" not in session.sent[0][0].headers

    def test_cursor_reserved_characters_stay_in_params(self):
        source, session = _source(
            [
                _response(payload={"rows": [_row()], "next_cursor": "a=b&c+d"}),
                _response(
                    payload={
                        "rows": [_row("2026-07-01T00:00:01Z")],
                        "next_cursor": None,
                    }
                ),
            ]
        )
        list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert parse_qs(urlsplit(session.sent[1][0].url).query)["cursor"] == [
            "a=b&c+d"
        ]

    def test_injected_session_is_never_closed(self):
        source, session = _source([])
        source.close()
        source.close()
        assert session.close_calls == 0

    def test_owned_session_closes_once(self, monkeypatch):
        session = ScriptedSession([])
        monkeypatch.setattr(
            "importer.fetch_source_http.requests.Session", lambda: session
        )
        source = HttpSource("https://example.test")
        source.close()
        source.close()
        assert session.close_calls == 1

    def test_batch_size_bounds_are_enforced(self):
        with pytest.raises(ValueError, match="positive"):
            HttpSource("https://example.test", batch_size=0)
        with pytest.raises(ValueError, match="10000"):
            HttpSource("https://example.test", batch_size=10001)


class TestBulkPages:
    def test_pagination_and_naive_timestamps_without_id(self):
        source, _ = _source(
            [
                _response(payload={"rows": [_row()], "next_cursor": "c1"}),
                _response(
                    payload={
                        "rows": [_row("2026-07-01T00:00:01Z", extra="ignored")],
                        "next_cursor": None,
                    }
                ),
            ]
        )
        batches = list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert [row["timestamp"] for batch in batches for row in batch] == [
            _T0,
            _T0 + timedelta(seconds=1),
        ]
        assert "extra" not in batches[1][0]
        assert "id" not in batches[0][0]

    @pytest.mark.parametrize(
        "payload,match",
        [
            ({"rows": []}, "rows and next_cursor"),
            ({"rows": {}, "next_cursor": None}, "rows must be a list"),
            ({"rows": [], "next_cursor": 1}, "string or null"),
            ({"rows": [1], "next_cursor": None}, "contain objects"),
            (
                {"rows": [{"symbol": "BTCUSDT"}], "next_cursor": None},
                "missing a required",
            ),
            (
                {"rows": [_row(symbol=1)], "next_cursor": None},
                "symbol must be",
            ),
            (
                {"rows": [_row(last_price=True)], "next_cursor": None},
                "finite number",
            ),
            (
                {"rows": [_row(last_price=float("nan"))], "next_cursor": None},
                "finite number",
            ),
        ],
    )
    def test_schema_errors(self, payload, match):
        source, _ = _source([_response(payload=payload)])
        with pytest.raises(HttpSourceError, match=match):
            list(source.fetch_batches("BTCUSDT", _T0, _T1))

    def test_damaged_rows_filter_and_continue(self, caplog):
        source, _ = _source(
            [
                _response(
                    payload={
                        "rows": [
                            _row(symbol=None),
                            _row(symbol="ETHUSDT"),
                            _row(ts=None),
                            _row(ts="not-a-time"),
                        ],
                        "next_cursor": "next",
                    }
                ),
                _response(
                    payload={
                        "rows": [_row("2026-07-01T00:00:01Z")],
                        "next_cursor": None,
                    }
                ),
            ]
        )
        with caplog.at_level(logging.WARNING):
            batches = list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert len(batches) == 1
        records = [r for r in caplog.records if "damaged-row filters" in r.message]
        assert len(records) == 1
        assert all(f"'{'null_symbol'}': 1" not in r.message for r in [])
        assert "'null_symbol': 1" in records[0].message

    @pytest.mark.parametrize(
        "timestamp",
        ["2026-06-30T23:59:59Z", "2026-07-01T01:00:01Z"],
    )
    def test_out_of_window_rejected(self, timestamp):
        source, _ = _source(
            [_response(payload={"rows": [_row(timestamp)], "next_cursor": None})]
        )
        with pytest.raises(HttpSourceError, match="outside"):
            list(source.fetch_batches("BTCUSDT", _T0, _T1))

    def test_decreasing_inside_or_across_pages_rejected(self):
        inside, _ = _source(
            [
                _response(
                    payload={
                        "rows": [
                            _row("2026-07-01T00:00:02Z"),
                            _row("2026-07-01T00:00:01Z"),
                        ],
                        "next_cursor": None,
                    }
                )
            ]
        )
        with pytest.raises(HttpSourceError, match="decreased"):
            list(inside.fetch_batches("BTCUSDT", _T0, _T1))

        across, _ = _source(
            [
                _response(
                    payload={
                        "rows": [_row("2026-07-01T00:00:02Z")],
                        "next_cursor": "c1",
                    }
                ),
                _response(
                    payload={
                        "rows": [_row("2026-07-01T00:00:01Z")],
                        "next_cursor": None,
                    }
                ),
            ]
        )
        with pytest.raises(HttpSourceError, match="decreased"):
            list(across.fetch_batches("BTCUSDT", _T0, _T1))

    def test_cursor_progress_rules(self):
        empty, _ = _source(
            [_response(payload={"rows": [], "next_cursor": "bad"})]
        )
        with pytest.raises(HttpSourceError, match="empty rows"):
            list(empty.fetch_batches("BTCUSDT", _T0, _T1))

        repeated, _ = _source(
            [
                _response(payload={"rows": [_row()], "next_cursor": "c1"}),
                _response(
                    payload={
                        "rows": [_row("2026-07-01T00:00:01Z")],
                        "next_cursor": "c1",
                    }
                ),
            ]
        )
        yielded = repeated.fetch_batches("BTCUSDT", _T0, _T1)
        next(yielded)
        with pytest.raises(HttpSourceError, match="cycled"):
            next(yielded)

    def test_empty_string_is_opaque_cursor(self):
        source, session = _source(
            [
                _response(payload={"rows": [_row()], "next_cursor": ""}),
                _response(
                    payload={
                        "rows": [_row("2026-07-01T00:00:01Z")],
                        "next_cursor": None,
                    }
                ),
            ]
        )
        assert len(list(source.fetch_batches("BTCUSDT", _T0, _T1))) == 2
        assert parse_qs(
            urlsplit(session.sent[1][0].url).query, keep_blank_values=True
        )["cursor"] == [""]


class TestErrorsAndRetries:
    @pytest.mark.parametrize("status", [401, 404, 422, 302, 204, 418])
    def test_terminal_statuses(self, status):
        source, session = _source([_response(status=status, raw=b"failure")])
        with pytest.raises(HttpSourceError, match=str(status)):
            list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert len(session.sent) == 1

    @pytest.mark.parametrize("status", [429, 500, 599])
    def test_retryable_statuses_reuse_parameters(self, status):
        source, session = _source(
            [
                _response(status=status),
                _response(payload={"rows": [], "next_cursor": None}),
            ]
        )
        assert list(source.fetch_batches("BTCUSDT", _T0, _T1)) == []
        assert session.sent[0][0].url == session.sent[1][0].url

    @pytest.mark.parametrize(
        "exc",
        [
            requests.ConnectTimeout("x"),
            requests.ReadTimeout("x"),
            requests.ConnectionError("x"),
            requests.exceptions.ChunkedEncodingError("x"),
        ],
    )
    def test_retryable_request_exceptions(self, exc):
        source, session = _source(
            [exc, _response(payload={"rows": [], "next_cursor": None})]
        )
        assert list(source.fetch_batches("BTCUSDT", _T0, _T1)) == []
        assert len(session.sent) == 2

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.SSLError("x"),
            requests.exceptions.ProxyError("x"),
            requests.exceptions.InvalidURL("x"),
            requests.exceptions.MissingSchema("x"),
            requests.exceptions.InvalidSchema("x"),
            requests.exceptions.InvalidHeader("x"),
            requests.RequestException("x"),
        ],
    )
    def test_terminal_request_exceptions(self, exc):
        source, session = _source([exc])
        with pytest.raises(HttpSourceError):
            list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert len(session.sent) == 1

    def test_retry_after_delta_date_and_over_cap(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(
            "importer.fetch_source_http.time.sleep", sleeps.append
        )
        source, _ = _source(
            [
                _response(status=429, headers={"Retry-After": "3"}),
                _response(payload={"rows": [], "next_cursor": None}),
            ]
        )
        list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert sleeps == [3]

        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        monkeypatch.setattr("importer.fetch_source_http._utc_now", lambda: now)
        source, _ = _source(
            [
                _response(
                    status=429,
                    headers={
                        "Retry-After": format_datetime(
                            now + timedelta(seconds=4), usegmt=True
                        )
                    },
                ),
                _response(payload={"rows": [], "next_cursor": None}),
            ]
        )
        list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert sleeps[-1] == 4

        source, _ = _source(
            [_response(status=429, headers={"Retry-After": "31"})]
        )
        with pytest.raises(HttpSourceError, match="exceeds"):
            list(source.fetch_batches("BTCUSDT", _T0, _T1))

    @pytest.mark.parametrize(
        "response,match",
        [
            (
                _response(
                    payload={"rows": [], "next_cursor": None},
                    content_type="text/plain",
                ),
                "application/json",
            ),
            (
                _response(raw=b"{", content_type="application/json"),
                "invalid JSON",
            ),
            (_response(payload=[]), "top-level object"),
        ],
    )
    def test_terminal_200_protocol_errors_are_not_retried(self, response, match):
        source, session = _source([response])
        with pytest.raises(HttpSourceError, match=match):
            list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert len(session.sent) == 1

    def test_secret_redacted_from_body_exception_and_traceback(self):
        secret = "very-secret-token"
        source, _ = _source(
            [
                _response(
                    status=401,
                    payload={"detail": ["prefix", secret, "suffix"]},
                )
            ],
            api_key=secret,
        )
        with pytest.raises(HttpSourceError) as caught:
            list(source.fetch_batches("BTCUSDT", _T0, _T1))
        rendered = "".join(traceback.format_exception(caught.value))
        assert secret not in str(caught.value)
        assert secret not in rendered

        source, _ = _source([RuntimeError(f"boom {secret}")], api_key=secret)
        with pytest.raises(HttpSourceError) as caught:
            list(source.fetch_batches("BTCUSDT", _T0, _T1))
        assert secret not in "".join(traceback.format_exception(caught.value))


class TestKlines:
    def _kline(self, ts="2026-07-01T00:00:00Z", **overrides):
        row = {
            "start_time": ts,
            "open": 1,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
        }
        row.update(overrides)
        return row

    def test_authenticated_shared_transport_and_gaps(self):
        source, session = _source(
            [
                _response(
                    payload={
                        "rows": [
                            self._kline(),
                            self._kline("2026-07-01T00:02:00Z"),
                        ]
                    }
                )
            ],
            api_key="token",
        )
        rows = source.fetch_klines("BTCUSDT", _T0, _T0 + timedelta(days=1))
        assert [row["start_time"] for row in rows] == [
            _T0,
            _T0 + timedelta(minutes=2),
        ]
        request, kwargs = session.sent[0]
        assert "/deploy/klines?" in request.url
        assert parse_qs(urlsplit(request.url).query)["interval"] == ["1m"]
        assert request.headers["Authorization"] == "Bearer token"
        assert kwargs["timeout"] == (5, 30)

    @pytest.mark.parametrize(
        "payload,match",
        [
            ({}, "rows"),
            ({"rows": {}}, "rows"),
            ({"rows": [1]}, "objects"),
            ({"rows": [{"start_time": "x"}]}, "missing"),
            (
                {"rows": [{"start_time": "x", "open": True, "high": 1, "low": 1, "close": 1}]},
                "finite",
            ),
            (
                {"rows": [{"start_time": "x", "open": 1, "high": 1, "low": 1, "close": 1}]},
                "ending in Z",
            ),
        ],
    )
    def test_kline_schema(self, payload, match):
        source, _ = _source([_response(payload=payload)])
        with pytest.raises(HttpSourceError, match=match):
            source.fetch_klines("BTCUSDT", _T0, _T0 + timedelta(days=1))

    def test_minute_order_and_window_rules(self):
        for rows, match in [
            ([self._kline("2026-07-01T00:00:01Z")], "whole UTC minute"),
            ([self._kline(), self._kline()], "strictly increasing"),
            ([self._kline("2026-07-02T00:00:00Z")], "outside"),
        ]:
            source, _ = _source([_response(payload={"rows": rows})])
            with pytest.raises(HttpSourceError, match=match):
                source.fetch_klines("BTCUSDT", _T0, _T0 + timedelta(days=1))

        source, _ = _source([])
        with pytest.raises(HttpSourceError, match="at most 24 hours"):
            source.fetch_klines(
                "BTCUSDT", _T0, _T0 + timedelta(days=1, minutes=1)
            )
