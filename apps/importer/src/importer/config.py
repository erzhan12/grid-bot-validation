"""CLI parsing, HTTP preflight, and datetime discipline for the importer.

Every datetime entering the pipeline is normalized to naive UTC (convert to
UTC, strip tzinfo). SQLite returns naive datetimes, so any aware value that
survives into a comparison against a stored cursor raises ``TypeError`` —
the same guard replay applies before comparing timestamps.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import SplitResult, urlsplit, urlunsplit

import requests

_ASCII_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN68_RE = re.compile(r"^[A-Za-z0-9\-._~+/]+={0,2}$")


class ConfigurationError(ValueError):
    """A safe-to-display importer configuration error."""


@dataclass(frozen=True)
class HttpPreflight:
    """Non-secret, locally validated HTTP source identity."""

    canonical_base_url: str
    origin_label: str
    source_fingerprint: str


def to_naive_utc(dt: datetime) -> datetime:
    """Convert to UTC and strip tzinfo; naive input passes through unchanged.

    Naive datetimes are assumed UTC already (owner-confirmed source
    semantics).
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 datetime string and return it as naive UTC.

    Accepts the trailing-``Z`` shorthand and explicit offsets
    (``+05:00``); aware input is converted to UTC before tzinfo is
    stripped.
    """
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid ISO-8601 datetime: {value!r}"
        ) from e
    return to_naive_utc(dt)


def positive_int(value: str) -> int:
    """argparse type: strictly positive integer (LIMIT 0/-1 are footguns)."""
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def unit_fraction(value: str) -> float:
    """argparse type: float in (0, 1] (0 would disable the OHLC value gate)."""
    number = float(value)
    if not 0 < number <= 1:
        raise argparse.ArgumentTypeError("must be in (0, 1]")
    return number


def parse_symbols(value: str) -> list[str]:
    """Split a comma-separated symbol list without changing symbol case."""
    symbols = [s.strip() for s in value.split(",") if s.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("--symbols must name at least one symbol")
    return symbols


def _safe_hostname(parts: SplitResult) -> tuple[str, int | None]:
    """Return a validated hostname/port without echoing rejected input."""
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ConfigurationError("--source-url has an invalid host or port") from None
    if not hostname:
        raise ConfigurationError("--source-url must include a host")
    try:
        if ":" in hostname:
            ipaddress.IPv6Address(hostname)
        else:
            ascii_host = hostname.encode("idna").decode("ascii")
            labels = (
                ascii_host[:-1].split(".")
                if ascii_host.endswith(".")
                else ascii_host.split(".")
            )
            if (
                len(ascii_host) > 253
                or any(
                    not label
                    or len(label) > 63
                    or label.startswith("-")
                    or label.endswith("-")
                    or not all(ch.isalnum() or ch == "-" for ch in label)
                    for label in labels
                )
            ):
                raise ValueError
            if all(ch.isdigit() or ch == "." for ch in ascii_host):
                ipaddress.IPv4Address(ascii_host)
    except (UnicodeError, ValueError):
        raise ConfigurationError("--source-url has an invalid host") from None
    return hostname, port


def _without_default_port(url: str) -> str:
    """Remove only an explicitly prepared default HTTP(S) authority port."""
    parts = urlsplit(url)
    hostname, port = _safe_hostname(parts)
    default = (parts.scheme == "http" and port == 80) or (
        parts.scheme == "https" and port == 443
    )
    if not default:
        return url
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    return urlunsplit((parts.scheme, rendered_host, parts.path, "", ""))


def preflight_http(source_url: str) -> HttpPreflight:
    """Validate and canonicalize an HTTP API base URL without network I/O."""
    if source_url != source_url.strip() or _ASCII_CONTROL_RE.search(source_url):
        raise ConfigurationError("--source-url contains unsafe whitespace or controls")
    try:
        parts = urlsplit(source_url)
    except ValueError:
        raise ConfigurationError("--source-url is malformed") from None
    if parts.scheme.lower() not in {"http", "https"}:
        raise ConfigurationError("--source-url must use http or https")
    if parts.username is not None or parts.password is not None:
        raise ConfigurationError("--source-url must not contain credentials")
    if "?" in source_url:
        raise ConfigurationError("--source-url must not contain a query")
    if "#" in source_url:
        raise ConfigurationError("--source-url must not contain a fragment")
    _safe_hostname(parts)

    # Give requests the exact slash-normalized base spelling that production
    # calls will use, then make its PreparedRequest URL the canonical authority.
    candidate = source_url if source_url.endswith("/") else source_url + "/"
    try:
        prepared = requests.Request("GET", candidate).prepare()
        canonical_with_slash = prepared.url
    except (requests.RequestException, UnicodeError, ValueError):
        raise ConfigurationError(
            "--source-url cannot be prepared as an HTTP URL"
        ) from None
    if canonical_with_slash is None:  # defensive: PreparedRequest.url is optional
        raise ConfigurationError("--source-url cannot be prepared as an HTTP URL")

    canonical_with_slash = _without_default_port(canonical_with_slash)
    canonical_base = canonical_with_slash[:-1]
    try:
        stable = requests.Request("GET", canonical_base + "/").prepare().url
        endpoint = requests.Request(
            "GET", canonical_base + "/ticker_data"
        ).prepare().url
    except (requests.RequestException, UnicodeError, ValueError):
        raise ConfigurationError(
            "--source-url cannot form protocol endpoints"
        ) from None
    if (
        stable != canonical_base + "/"
        or endpoint != canonical_base + "/ticker_data"
    ):
        raise ConfigurationError("--source-url is not stable after normalization")

    canonical_parts = urlsplit(canonical_base)
    hostname, port = _safe_hostname(canonical_parts)
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = rendered_host if port is None else f"{rendered_host}:{port}"
    origin_label = f"http:{canonical_parts.scheme}://{authority}"
    fingerprint = hashlib.sha256(canonical_base.encode("utf-8")).hexdigest()
    return HttpPreflight(canonical_base, origin_label, fingerprint)


def preflight_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> argparse.Namespace:
    """Apply source-aware checks before secrets, files, or network are touched."""
    if args.source == "db":
        args.symbols = [symbol.upper() for symbol in args.symbols]
        args.http_base_url = None
        args.http_origin_label = None
        args.http_source_fingerprint = None
        return args

    if args.start is None or args.end is None:
        parser.error("--source http requires both --start and --end")
    if args.start > args.end:
        parser.error("--start must not be after --end for --source http")
    if args.batch_size > 10000:
        parser.error("--batch-size for --source http must be in 1..10000")
    if any(_ASCII_CONTROL_RE.search(symbol) for symbol in args.symbols):
        parser.error("--symbols contains an unsafe HTTP symbol")
    try:
        preflight = preflight_http(args.source_url)
    except ConfigurationError as exc:
        parser.error(str(exc))
    args.http_base_url = preflight.canonical_base_url
    args.http_origin_label = preflight.origin_label
    args.http_source_fingerprint = preflight.source_fingerprint
    return args


def load_market_data_api_key() -> str | None:
    """Read and locally validate ``MARKET_DATA_API_KEY`` exactly once."""
    key = os.environ.get("MARKET_DATA_API_KEY")
    if key is None or key == "":
        return None
    if (
        key != key.strip()
        or _ASCII_CONTROL_RE.search(key)
        or _TOKEN68_RE.fullmatch(key) is None
    ):
        raise ConfigurationError("MARKET_DATA_API_KEY is not a valid Bearer token")
    try:
        key.encode("ascii")
        prepared = requests.Request(
            "GET",
            "https://market-data.invalid/ticker_data",
            headers={"Authorization": f"Bearer {key}"},
        ).prepare()
    except (UnicodeError, requests.RequestException, ValueError):
        # Drop the cause chain: prepare() may embed the Authorization header.
        raise ConfigurationError(
            "MARKET_DATA_API_KEY cannot be used as an HTTP header"
        ) from None
    if prepared.headers.get("Authorization") != f"Bearer {key}":
        raise ConfigurationError(
            "MARKET_DATA_API_KEY cannot be used as an HTTP header"
        )
    return key


class ImporterArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that completes source-aware, non-secret preflight."""

    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        return preflight_args(self, parsed)


def build_parser() -> argparse.ArgumentParser:
    """Build the importer CLI parser (invoked via ``python -m importer.main``)."""
    parser = ImporterArgumentParser(
        prog="python -m importer.main",
        description=(
            "One-way import: trad_save_history ticker_data -> per-symbol "
            "replay-compatible SQLite DBs. Imported DBs are "
            "for counterfactual A/B / relative ranking only."
        ),
    )
    parser.add_argument(
        "--source",
        required=True,
        choices=("db", "http"),
        help="Source transport: direct SQLAlchemy URL (db) or HTTP API (http).",
    )
    parser.add_argument(
        "--source-url",
        required=True,
        help=(
            "Transport A: SQLAlchemy URL (postgresql://... or sqlite:///path). "
            "Transport B: HTTP API base URL."
        ),
    )
    parser.add_argument(
        "--symbols",
        required=True,
        type=parse_symbols,
        help="Comma-separated symbols to import (e.g. BTCUSDT,ETHUSDT).",
    )
    parser.add_argument(
        "--start",
        type=parse_utc,
        default=None,
        help=(
            "ISO-8601 UTC start (inclusive). Default: source MIN(timestamp) "
            "probe (transport A only; http requires explicit bounds)."
        ),
    )
    parser.add_argument(
        "--end",
        type=parse_utc,
        default=None,
        help=(
            "ISO-8601 UTC end (inclusive). Default: source MAX(timestamp) "
            "probe (transport A only; http requires explicit bounds)."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default="data",
        help="Output directory for imported_<symbol>[_<tag>].db files.",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help=(
            "Optional filename tag for an isolated fresh import "
            "(imported_<symbol>_<tag>.db), e.g. while a sweep reads the "
            "default file."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=10000,
        help="Source fetch batch size (default 10000).",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run post-import validation (OHLC cross-check, smoke replay, "
        "recorder overlap probe).",
    )
    parser.add_argument(
        "--ohlc-threshold",
        type=unit_fraction,
        default=0.99,
        help="Fraction of exactly-matching OHLC buckets required to pass "
        "(default 0.99).",
    )
    parser.add_argument(
        "--recorder-db",
        default=None,
        help="Recorder SQLite path/URL for the --validate overlap probe "
        "(skipped with a NOTICE when omitted or no overlap).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser
