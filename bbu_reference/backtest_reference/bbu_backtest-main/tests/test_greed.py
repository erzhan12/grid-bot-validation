"""Unit tests for src/greed.py and the Greed class."""

import sys
from pathlib import Path

import pytest

# Ensure we can import modules from src/
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import greed  # noqa: E402  (after path manipulation)

# def _fake_round(_symbol: str, price: float) -> float:
#     """Deterministic price rounding stub used in tests."""
#     return round(price, 2)


class _Strat:
    def __init__(self, id_: int):
        self.id = id_


# @pytest.fixture(autouse=True)
# def patch_eval(monkeypatch):
#     """Patch eval in greed module to avoid using string-based eval of external API."""

#     def _eval_stub(path: str):  # path is ignored; we always return our stub
#         return _fake_round

#     monkeypatch.setattr(greed, "eval", _eval_stub)


def test_build_greed_basic_structure():
    g = greed.Greed(strat=_Strat(1), symbol="BTCUSDT", n=5, step=10)
    g.build_greed(last_close=100.0)

    # Expect 5 entries: 2 buys, 1 wait (middle), 2 sells
    assert len(g.greed) == 5

    # Middle entry is WAIT at the rounded last_close
    mid = g.greed[2]
    assert mid["side"] == g.WAIT
    assert mid["price"] == pytest.approx(100.0)

    # Prices should be sorted ascending
    assert g.is_price_sorted() is True

    # BUY ... WAIT ... SELL sequence should be valid
    assert g.is_greed_correct() is True


"""Update greed with last_filled_price is None"""


def test_update_greed_with_last_filled_price_is_none():
    g = greed.Greed(strat=_Strat(1), symbol="BTCUSDT", n=5, step=10)
    g.build_greed(last_close=100.0)
    greed_old = g.greed
    g.update_greed(last_filled_price=None, last_close=100.0)
    # Check that nothing changed
    assert g.greed == greed_old


"""Update greed with last_close is None"""


def test_update_greed_with_last_close_is_none():
    g = greed.Greed(strat=_Strat(1), symbol="BTCUSDT", n=5, step=10)
    g.build_greed(last_close=100.0)
    greed_old = g.greed
    g.update_greed(last_filled_price=100.0, last_close=None)
    # Check that nothing changed
    assert g.greed == greed_old


"""Update greed with last_filled_price and last_close are not None"""


def test_update_greed_sets_sides_relative_to_last_close():
    g = greed.Greed(strat=_Strat(1), symbol="BTCUSDT", n=5, step=10)
    g.build_greed(last_close=100.0)

    # last_filled at middle price; last_close equal to middle
    g.update_greed(last_filled_price=100.0, last_close=100.0)

    # Middle remains WAIT, below become BUY, above become SELL
    for entry in g.greed:
        if entry["price"] < 100.0:
            assert entry["side"] == g.BUY
        elif entry["price"] > 100.0:
            assert entry["side"] == g.SELL
        else:
            assert entry["side"] == g.WAIT


"""Update greed with rebuild_greed"""


def test_update_greed_with_rebuild_greed():
    g = greed.Greed(strat=_Strat(1), symbol="BTCUSDT", n=5, step=10)
    g.build_greed(last_close=100.0)
    g.update_greed(last_filled_price=200.0, last_close=200.0)
    assert g.greed[0]["price"] < 200.0
    assert g.greed[-1]["price"] > 200.0


"""Test is_too_close threshold"""


def test_is_too_close_threshold():
    g = greed.Greed(strat=_Strat(1), symbol="BTCUSDT", n=5, step=10)
    # step=10 -> threshold is greed_step/4 = 2.5%
    base = 100.0
    assert g.is_too_close(base * 1.01, base) is True   # 1% < 2.5%
    assert g.is_too_close(base * 1.03, base) is False  # 3% > 2.5%


"""Test center_grid rebalances on imbalance"""


def test_center_grid_rebalances_on_imbalance():
    g = greed.Greed(strat=_Strat(1), symbol="BTCUSDT", n=10, step=10)

    # Create an imbalanced grid: more BUYs than SELLs
    # Prices must be ascending
    g.greed = []
    g.greed.append({"side": g.BUY, "price": 95.00})
    g.greed.append({"side": g.BUY, "price": 96.00})
    g.greed.append({"side": g.BUY, "price": 97.00})
    g.greed.append({"side": g.BUY, "price": 98.00})
    g.greed.append({"side": g.BUY, "price": 99.00})
    g.greed.append({"side": g.WAIT, "price": 100.00})
    g.greed.append({"side": g.SELL, "price": 101.00})
    g.greed.append({"side": g.SELL, "price": 102.00})

    # Pre-check imbalance
    buy_count = len([e for e in g.greed if e["side"] == g.BUY])
    sell_count = len([e for e in g.greed if e["side"] == g.SELL])
    assert (buy_count - sell_count) / (buy_count + sell_count) > 0.3

    # center_grid should remove one bottom BUY and append a new SELL above highest sell
    g.center_grid()

    buy_count2 = len([e for e in g.greed if e["side"] == g.BUY])
    sell_count2 = len([e for e in g.greed if e["side"] == g.SELL])
    assert buy_count2 == buy_count - 1
    assert sell_count2 == sell_count + 1

    # Highest price should have increased by approx step
    highest_prices = [e["price"] for e in g.greed if e["side"] == g.SELL]
    assert max(highest_prices) >= 102.0


