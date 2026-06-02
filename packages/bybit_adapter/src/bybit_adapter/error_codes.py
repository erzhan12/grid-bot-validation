"""Shared Bybit error-code constants.

Centralises numeric ErrCode literals so classifiers and divergence
detectors reference one symbol instead of a magic number. Mirrors the
``AUTH_ERROR_CODES`` set kept in ``gridbot.executor``.
"""

# ErrCode 110017 "orderQty will be truncated to zero": Bybit dynamically
# clamps a reduce-only qty to the current exchange-side position size; when
# the position is smaller than the intent the clamp drops to zero and the
# order is rejected. Surfaces during local-mirror divergence (feature 0064,
# issue #149).
ORDER_QTY_TRUNCATED_TO_ZERO = 110017
