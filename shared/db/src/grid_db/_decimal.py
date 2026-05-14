"""Wallet-payload helpers shared by recorder and writer code."""

from decimal import Decimal


WALLET_ACCOUNT_JSON_KEYS: tuple[str, ...] = (
    "accountType",
    "marginMode",
    "totalEquity",
    "totalAvailableBalance",
    "totalMarginBalance",
    "accountIMRate",
    "accountMMRate",
)


def decimal_or_zero(value: object) -> Decimal:
    """Coerce a numeric Bybit field to Decimal, mapping missing/empty to zero.

    Bybit wallet payloads can carry ``""`` for some UTA account and coin fields.
    Keep malformed non-empty values as errors so callers can drop bad rows via
    their existing warning paths.
    """
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))
