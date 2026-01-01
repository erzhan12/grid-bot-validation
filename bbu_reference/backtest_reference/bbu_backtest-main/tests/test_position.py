"""Unit tests for the Position class."""

from src.position import Direction, Position


class _Strat:
    def __init__(self, id_=1, min_liq=1.0, max_liq=1.2, max_margin=10.0, min_total_margin=0.5):
        self.id = id_
        self.liq_ratio = {"min": min_liq, "max": max_liq}
        self.max_margin = max_margin
        self.min_total_margin = min_total_margin


def test_position_init():
    strat = _Strat()
    position = Position(Direction.LONG, strat)
    assert position is not None
    assert position.position_ratio == 1


def test_calc_amount_multiplier_calls_long(monkeypatch):
    """Ensure BUY branch is executed."""
    strat = _Strat()
    position = Position(Direction.LONG, strat)

    called = {"long": False}

    def fake_long(pos, last_close, entry_price, base_amount=None, min_amount=None):  # pragma: no cover - simple flag setter
        called["long"] = True

    monkeypatch.setattr(position, "_Position__calc_multiplier_long", fake_long)
    position.calc_amount_multiplier({"entryPrice": "1", "leverage": "1"}, 1.0)

    assert called["long"] is True


def test_calc_amount_multiplier_calls_short(monkeypatch):
    """Ensure SELL branch is executed."""
    strat = _Strat()
    position = Position(Direction.SHORT, strat)

    called = {"short": False}

    def fake_short(pos, last_close, entry_price, base_amount=None, min_amount=None):  # pragma: no cover - simple flag setter
        called["short"] = True

    monkeypatch.setattr(position, "_Position__calc_multiplier_short", fake_short)
    position.calc_amount_multiplier({"entryPrice": "1", "leverage": "1"}, 1.0)

    assert called["short"] is True