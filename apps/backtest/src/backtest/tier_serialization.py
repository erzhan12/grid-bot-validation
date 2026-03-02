"""Serialization helpers for MMTiers ↔ JSON-compatible dicts.

Used by ``RiskLimitProvider`` when reading/writing the local cache file.
"""

from decimal import Decimal

from gridcore.pnl import MMTiers


def tiers_to_dict(tiers: MMTiers) -> list[dict[str, str]]:
    """Serialize MMTiers to JSON-compatible list of dicts."""
    return [
        {
            "max_value": str(max_val),
            "mmr_rate": str(mmr_rate),
            "deduction": str(deduction),
            "imr_rate": str(imr_rate),
        }
        for max_val, mmr_rate, deduction, imr_rate in tiers
    ]


def tiers_from_dict(tier_dicts: list[dict[str, str]]) -> MMTiers:
    """Deserialize MMTiers from cached list of dicts.

    Handles old cache files that lack ``imr_rate`` by defaulting to "0".

    Raises:
        ValueError: If any tier dict is missing required keys.
    """
    _REQUIRED_KEYS = {"max_value", "mmr_rate", "deduction"}
    result = []
    for d in tier_dicts:
        if not _REQUIRED_KEYS.issubset(d.keys()):
            missing = _REQUIRED_KEYS - set(d.keys())
            raise ValueError(f"Missing required tier keys: {missing}")
        result.append((
            Decimal(d["max_value"]),
            Decimal(d["mmr_rate"]),
            Decimal(d["deduction"]),
            Decimal(d.get("imr_rate", "0")),
        ))
    return result
