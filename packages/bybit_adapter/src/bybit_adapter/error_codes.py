"""Shared Bybit error-code constants.

Centralises numeric ErrCode literals so classifiers and divergence
detectors reference one symbol instead of a magic number. Mirrors the
``AUTH_ERROR_CODES`` set kept in ``gridbot.executor``.

Bybit error-code reference (look up new codes / wording here):
https://bybit-exchange.github.io/docs/v5/error
"""

# ErrCode 110017 "orderQty will be truncated to zero": Bybit dynamically
# clamps a reduce-only qty to the current exchange-side position size; when
# the position is smaller than the intent the clamp drops to zero and the
# order is rejected. Surfaces during local-mirror divergence (feature 0064,
# issue #149).
ORDER_QTY_TRUNCATED_TO_ZERO = 110017

# ErrCode 110007 "available balance not enough for new order": the account's
# free margin cannot cover an OPEN (non-reduce-only) order. Reduce-only orders
# are exempt (they free margin). Drives the low-balance preflight + retry-queue
# no-enqueue guard (feature 0066, issue #159).
INSUFFICIENT_BALANCE = 110007
