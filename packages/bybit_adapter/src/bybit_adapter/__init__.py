"""Bybit-specific adapter for WebSocket and REST API integration.

This package provides:
- Event normalization from Bybit WebSocket messages to gridcore events
- WebSocket client management for public and private streams
- REST API client for gap reconciliation
- Per-account rate limiting
"""

from bybit_adapter.normalizer import BybitNormalizer
from bybit_adapter.ws_client import (
    ConnectionState,
    PublicWebSocketClient,
    PrivateWebSocketClient,
)
from bybit_adapter.rest_client import BybitRestClient
from bybit_adapter.rate_limiter import RateLimiter, RateLimitConfig
from bybit_adapter.error_codes import ORDER_QTY_TRUNCATED_TO_ZERO

__all__ = [
    "BybitNormalizer",
    "ConnectionState",
    "PublicWebSocketClient",
    "PrivateWebSocketClient",
    "BybitRestClient",
    "RateLimiter",
    "RateLimitConfig",
    "ORDER_QTY_TRUNCATED_TO_ZERO",
]
