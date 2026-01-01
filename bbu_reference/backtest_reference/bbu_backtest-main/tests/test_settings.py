"""Unit tests for config/settings.py components."""

from typing import Any, Dict

import pytest


def test_amount_numeric_amount_parses_x_prefix(monkeypatch):
    from config.settings import Amount

    amt = Amount(name="risk_small", amount="x0.005", strat=1)
    assert pytest.approx(amt.numeric_amount) == 0.005


def test_amount_numeric_amount_no_x_prefix(monkeypatch):
    from config.settings import Amount

    amt = Amount(name="risk_small", amount="0.005", strat=1)
    assert pytest.approx(amt.numeric_amount) == 0.005


def test_configdata_parsing_lists_to_models():
    from config.settings import ConfigData

    data: Dict[str, Any] = {
        "pair_timeframes": [
            {
                "id": 1,
                "strat": "s1",
                "symbol": "BTCUSDT",
                "greed_step": 0.25,
                "max_margin": 3,
                "greed_count": 10,
                "min_liq_ratio": 0.7,
                "max_liq_ratio": 1.1,
                "exchange": "bybit",
                "long_koef": 1.0,
                "min_total_margin": 2,
            }
        ],
        "amounts": [
            {"name": "risk_small", "amount": "x0.010", "strat": 1}
        ],
    }

    cfg = ConfigData(**data)
    assert len(cfg.pair_timeframes) == 1
    assert len(cfg.amounts) == 1
    assert cfg.amounts[0].numeric_amount == pytest.approx(0.01)


def test_settings_uses_loader_result(monkeypatch):
    """Patch the internal YAML loader to feed controlled data and assert it populates fields."""
    import config.settings as cfg_mod

    # Reset the singleton instance
    cfg_mod.Settings._instance = None

    sample_data = {
        "pair_timeframes": [
            {
                "id": 1,
                "strat": "s1",
                "symbol": "BTCUSDT",
            }
        ],
        "amounts": [
            {"name": "risk_small", "amount": "x0.010", "strat": 1}
        ],
    }

    # Patch the private loader to avoid filesystem dependency
    monkeypatch.setattr(
        cfg_mod.Settings, "_Settings__load_yaml_file", lambda *a, **k: sample_data
    )

    s = cfg_mod.Settings()
    s.read_settings()

    assert len(s.amounts) == 1
    assert len(s.pair_timeframes) == 1
    # Ensure Amount model parsed and numeric_amount works
    assert s.amounts[0].numeric_amount == pytest.approx(0.01)


