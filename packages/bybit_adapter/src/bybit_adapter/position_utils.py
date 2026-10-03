"""Helpers for Bybit position payloads shared across apps (recorder, gridbot)."""

# Hedge-mode positionIdx → leg side (1 = long, 2 = short).
_HEDGE_IDX_SIDE = {"1": "Buy", "2": "Sell"}


def leg_side(pos: dict) -> str:
    """Side of a position row; a flat hedge leg's side comes from positionIdx.

    Bybit sends ``side=""`` for an empty position (WS and REST), but in hedge
    mode the leg is still known from ``positionIdx``. One-way mode (``positionIdx`` 0)
    has no leg, so its flat row keeps the empty side (0110 B2a, 0111).
    """
    side = pos.get("side", "")
    if side:
        return side
    return _HEDGE_IDX_SIDE.get(str(pos.get("positionIdx", "")), "")
