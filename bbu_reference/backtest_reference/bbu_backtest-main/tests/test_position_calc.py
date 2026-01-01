"""Unit tests for Position.calc_amount_multiplier covering branch cases."""

from src.position import Position, PositionSide


class _Strat:
    def __init__(self, id_=1, min_liq=1.0, max_liq=1.2, max_margin=10.0, min_total_margin=0.5):
        self.id = id_
        self.liq_ratio = {"min": min_liq, "max": max_liq}
        self.max_margin = max_margin
        self.min_total_margin = min_total_margin


def make_pos(direction: str, strat: _Strat) -> Position:
    return Position(direction=direction, strat=strat)


def set_opposites(a: Position, b: Position) -> None:
    a.set_opposite(b)
    b.set_opposite(a)


def update_with(a: Position, position_value: float, wallet_balance: float, entry_price: float, 
                liq_price: float, leverage: float, last_close: float):
    pos = {
        "positionValue": str(position_value),
        "entryPrice": str(entry_price),
        "avgPrice": str(entry_price),
        "leverage": str(leverage),
        "liqPrice": str(liq_price),
        "size": "1",
    }
    a.update_position(position_response=pos, wallet_balance=wallet_balance, last_close=last_close)
    return pos


def test_long_liq_ratio_above_1_05_min_sets_sell_1_5():
    strat = _Strat(min_liq=1.0)
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    last = 100.0
    # ratio = 1.2 > 1.05 * min
    update_with(short, 10.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(long, 10.0, 100.0, 100.0, 120.0, 1.0, last)

    mult = long.get_amount_multiplier()
    assert mult[PositionSide.SELL] == 1.5


def test_long_liq_ratio_above_min_sets_opposite_buy_0_5():
    strat = _Strat(min_liq=1.0)
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    last = 100.0
    # ratio = 1.02 within (min, 1.05*min]
    update_with(short, 10.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(long, 10.0, 100.0, 100.0, 102.0, 1.0, last)

    mult_short = short.get_amount_multiplier()
    assert mult_short[PositionSide.BUY] == 0.5


def test_long_is_position_equal_and_low_total_margin_sets_sell_0_5():
    strat = _Strat(min_total_margin=0.5)
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    last = 100.0
    # Equal margins = 0.2 each (< 0.5)
    update_with(short, 20.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(long, 20.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(short, 20.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(long, 20.0, 100.0, 100.0, 0.0, 1.0, last)

    mult = long.get_amount_multiplier()
    assert mult[PositionSide.SELL] == 0.5


def test_long_low_ratio_and_negative_upnl_sets_buy_2():
    strat = _Strat()
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    # Set state to skip liq checks and equal/total checks
    long.position_ratio = 0.4
    last = 90.0
    # entry=100, last=90 => long upnl negative
    update_with(short, 50.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(long, 50.0, 100.0, 100.0, 0.0, 1.0, last)

    mult = long.get_amount_multiplier()
    assert mult[PositionSide.BUY] == 2


def test_long_very_low_ratio_sets_buy_2():
    strat = _Strat()
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    long.position_ratio = 0.1
    last = 110.0
    # entry=100, last=110 => upnl positive; triggers last branch
    update_with(short, 50.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(long, 50.0, 100.0, 100.0, 0.0, 1.0, last)

    mult = long.get_amount_multiplier()
    assert mult[PositionSide.BUY] == 2


def test_short_liq_ratio_below_0_95_max_sets_buy_1_5():
    strat = _Strat(max_liq=1.2)
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    last = 100.0
    # ratio = 1.0 < 0.95*1.2 = 1.14
    update_with(long, 10.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(short, 10.0, 100.0, 100.0, 100.0, 1.0, last)

    mult = short.get_amount_multiplier()
    assert mult[PositionSide.BUY] == 1.5


def test_short_liq_ratio_below_max_sets_opposite_sell_0_5():
    strat = _Strat(max_liq=1.2)
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    last = 100.0
    # ratio = 1.16 in (0, max) and above 0.95*max
    update_with(long, 10.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(short, 10.0, 100.0, 100.0, 116.0, 1.0, last)

    mult_long = long.get_amount_multiplier()
    assert mult_long[PositionSide.SELL] == 0.5


def test_short_is_position_equal_and_low_total_margin_sets_buy_0_5():
    strat = _Strat(min_total_margin=0.5)
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    last = 100.0
    # Equal margins = 0.2 each (< 0.5)
    update_with(long, 20.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(short, 20.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(long, 20.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(short, 20.0, 100.0, 100.0, 0.0, 1.0, last)

    mult = short.get_amount_multiplier()
    assert mult[PositionSide.BUY] == 0.5


def test_short_high_ratio_and_negative_upnl_sets_sell_2():
    strat = _Strat()
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    short.position_ratio = 2.1
    # short upnl negative when last > entry
    last = 110.0
    update_with(long, 50.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(short, 50.0, 100.0, 100.0, 0.0, 1.0, last)

    mult = short.get_amount_multiplier()
    assert mult[PositionSide.SELL] == 2


def test_short_very_high_ratio_sets_sell_2():
    strat = _Strat()
    long = make_pos("long", strat)
    short = make_pos("short", strat)
    set_opposites(long, short)

    short.position_ratio = 6.0
    # short upnl positive when last < entry
    last = 90.0
    update_with(long, 50.0, 100.0, 100.0, 0.0, 1.0, last)
    update_with(short, 50.0, 100.0, 100.0, 0.0, 1.0, last)

    mult = short.get_amount_multiplier()
    assert mult[PositionSide.SELL] == 2


