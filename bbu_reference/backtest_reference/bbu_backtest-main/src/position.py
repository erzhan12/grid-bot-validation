from dataclasses import dataclass
from typing import Any

from src.enums import Direction, PositionSide


@dataclass
class PositionStatus:
    """Represents the current state of a position."""

    side: "PositionSide"
    size: float = 0.0001
    entry_price: float | None = None
    liquidation_price: float | None = None
    # Placeholders for future attributes
    leverage: float | None = None
    position_value: float | None = None


class Position:
    def __init__(self, direction, strat):
        self.__direction = direction
        self.__status: PositionStatus | None = None
        self.__wallet_balance = None
        self.__margin = None
        self.__opposite = None
        self.__amount_multiplier = {PositionSide.BUY: 1.0, PositionSide.SELL: 1.0}
        self.__min_liq_ratio = strat.liq_ratio['min']
        self.__max_liq_ratio = strat.liq_ratio['max']
        self.__max_margin = strat.max_margin
        self.__min_total_margin = strat.min_total_margin
        self.__strat_id = strat.id
        self.__upnl = None
        self.position_ratio = 1
        self._api_context = None  # Reference to API for getting min_amount and base_amount

    def log_position(self, symbol, last_close):
        # Safely format values that might be None
        margin_str = f"{self.__margin:.2f}" if self.__margin is not None else "N/A"
        upnl_str = f"{self.__upnl:.2f}" if self.__upnl is not None else "N/A"
        ratio_str = f"{self.position_ratio:.2f}" if self.position_ratio is not None else "N/A"
        
        log = f'{symbol}-{self.__strat_id} {self.__direction} margin:{margin_str}\n' \
              f'liq_price:{self.liq_price:.2f} ratio:{self.get_liquidation_ratio(last_close):.2f}\n' \
              f'unrealised PnL:{upnl_str}%\n' \
              f'multiplier:{self.__amount_multiplier}\n' \
              f'position_ratio:{ratio_str}\n' \
              f'total margin: {self.get_total_margin():.2f}'
        print(log)

    def reset_amount_multiplier(self):
        self.set_amount_multiplier(PositionSide.BUY, 1.0)
        self.set_amount_multiplier(PositionSide.SELL, 1.0)
    
    def __calc_multiplier_long(self, pos, last_close, entry_price, base_amount=None, min_amount=None):
        self.__upnl = (1 / entry_price - 1 / last_close) * entry_price * 100 * float(pos['leverage'])
        if self.get_liquidation_ratio(last_close) > 1.05 * self.__min_liq_ratio:
            self.set_amount_multiplier(PositionSide.SELL, 1.5)  # decrease long position
        elif self.get_liquidation_ratio(last_close) > self.__min_liq_ratio:
            # if self.__opposite.get_margin() > self.__max_margin:
            #     self.set_amount_multiplier(Position.SIDE_SELL, 2.0)  # decrease long position
            # else:
            #     self.__opposite.set_amount_multiplier(Position.SIDE_SELL, 2.0)  # increase short position
            self.__opposite.set_amount_multiplier(PositionSide.BUY, 0.5)  # increase short position
        elif self.is_position_equal() and self.get_total_margin() < self.__min_total_margin:
            self.set_amount_multiplier(PositionSide.SELL, 0.5)  # increase long position
            # Check if order size with 0.5 multiplier equals minimum order size
            if self._is_order_at_minimum_size(base_amount, 0.5, last_close, min_amount):
                self.__opposite.set_amount_multiplier(PositionSide.SELL, 2.0)  # compensate with opposite direction
        elif self.position_ratio < 0.5 and self.__upnl < 0:
            self.set_amount_multiplier(PositionSide.BUY, 2)  # increase long position
        elif self.position_ratio < 0.20:
            self.set_amount_multiplier(PositionSide.BUY, 2)  # increase long position 

    def __calc_multiplier_short(self, pos, last_close, entry_price, base_amount=None, min_amount=None):
        self.__upnl = (1 / last_close - 1 / entry_price) * entry_price * 100 * float(pos['leverage'])
        if 0.0 < self.get_liquidation_ratio(last_close) < 0.95 * self.__max_liq_ratio:
            self.set_amount_multiplier(PositionSide.BUY, 1.5)  # decrease short position

        elif 0.0 < self.get_liquidation_ratio(last_close) < self.__max_liq_ratio:
            # if self.__opposite.get_margin() > self.__max_margin:
            #     self.set_amount_multiplier(Position.SIDE_BUY, 2.0)  # decrease short position
            # else:
            #     self.__opposite.set_amount_multiplier(Position.SIDE_BUY, 2.0)  # increase long position
            self.__opposite.set_amount_multiplier(PositionSide.SELL, 0.5)  # increase long position
        elif self.is_position_equal() and self.get_total_margin() < self.__min_total_margin:
            self.set_amount_multiplier(PositionSide.BUY, 0.5)  # increase short position
            # Check if order size with 0.5 multiplier equals minimum order size
            if self._is_order_at_minimum_size(base_amount, 0.5, last_close, min_amount):
                self.__opposite.set_amount_multiplier(PositionSide.BUY, 2.0)  # compensate with opposite direction
        elif self.position_ratio > 2.0 and self.__upnl < 0:
            self.set_amount_multiplier(PositionSide.SELL, 2)  # increase short position
        elif self.position_ratio > 5.0:
            self.set_amount_multiplier(PositionSide.SELL, 2)  # increase short position 

    def _is_order_at_minimum_size(self, base_amount, multiplier, price, min_amount):
        """
        Check if order size with multiplier equals minimum order size

        Args:
            base_amount: Base order amount before multiplier
            multiplier: Amount multiplier to apply
            price: Current price
            min_amount: Minimum order size for the symbol

        Returns:
            bool: True if resulting order equals minimum order size
        """
        if base_amount is None or price is None or min_amount is None:
            return False

        # Calculate what the order size would be after applying multiplier
        calculated_amount = base_amount * multiplier

        # Check if this equals the minimum order size (with small tolerance for floating point)
        tolerance = min_amount * 0.01  # 1% tolerance
        return abs(calculated_amount - min_amount) <= tolerance

    def calc_amount_multiplier(self, pos, last_close, base_amount=None, min_amount=None):
        # long
        try:
            entry_price = float(pos['entryPrice'])
        except KeyError:
            entry_price = float(pos['avgPrice'])

        # If parameters not provided, try to get them from API context
        if base_amount is None or min_amount is None:
            if self._api_context is not None:
                try:
                    # Get base amount from API context using the same method as order placement
                    if base_amount is None:
                        # Use a placeholder symbol - this is approximation since we don't have symbol here
                        symbol = getattr(self._api_context, 'symbol', 'BTCUSDT')
                        base_amount = self._api_context._BybitApiUsdt__get_amount(symbol, last_close, 'Buy', 'placeholder')
                    # Get min amount from API context
                    if min_amount is None:
                        min_amount = self._api_context.min_amount
                except (AttributeError, TypeError):
                    # If we can't get the values, proceed without the minimum order size check
                    pass

        if self.__direction == Direction.LONG:
            self.__calc_multiplier_long(pos, last_close, entry_price, base_amount, min_amount)
        # short
        if self.__direction == Direction.SHORT:
            self.__calc_multiplier_short(pos, last_close, entry_price, base_amount, min_amount)

    def set_amount_multiplier(self, side, mult):
        self.__amount_multiplier[side] = mult

    def get_amount_multiplier(self):
        return self.__amount_multiplier

    def update_position(self, position_response: dict[str, Any], wallet_balance, last_close, base_amount=None, min_amount=None):
        """Update internal status from a raw API response."""

        try:
            side_val = position_response.get('side')
            if side_val is not None:
                side = PositionSide(side_val)
            else:
                side = PositionSide.BUY if self.__direction == Direction.LONG else PositionSide.SELL

            status = PositionStatus(
                side=side,
                size=float(position_response['size']),
                entry_price=float(position_response.get('entryPrice') or position_response.get('avgPrice')),
                liquidation_price=float(position_response.get('liqPrice', 0.0)),
                leverage=float(position_response.get('leverage', 0.0)),
                position_value=float(position_response['positionValue']),
            )
            self.__status = status
            self.__wallet_balance = wallet_balance
            self.__margin = (status.position_value or 0.0) / wallet_balance
            self.calc_amount_multiplier(position_response, last_close, base_amount, min_amount)
        except (TypeError, KeyError, ValueError):
            self.__status = None
            self.__margin = 0

    def set_opposite(self, opposite):
        self.__opposite = opposite

    def set_api_context(self, api_context):
        """Set reference to API context for accessing min_amount and base_amount calculations"""
        self._api_context = api_context

    def is_empty(self):
        if self.__status is None:
            return True
        return False

    def get_margin(self):
        return self.__margin

    def get_liquidation_ratio(self, last_close):
        return self.liq_price / last_close

    def is_position_equal(self):
        try:
            return 0.94 < self.get_margin_ratio() < 1.05
        except ZeroDivisionError:
            return False

    def get_margin_ratio(self):
        margin1 = self.get_margin() if self.get_margin() is not None else 0.0
        margin2 = self.__opposite.get_margin() if self.__opposite.get_margin() is not None else 0.0
        
        if margin2 == 0:
            return 0.0  # Avoid division by zero
        
        return margin1 / margin2

    def get_total_margin(self):
        margin1 = self.__margin if self.__margin is not None else 0.0
        margin2 = self.__opposite.get_margin() if self.__opposite.get_margin() is not None else 0.0
        return margin1 + margin2

    @property
    def size(self):
        return 0.0001 if self.__status is None else self.__status.size
    
    @size.setter
    def size(self, value):
        if self.__status is None:
            # Initialize status if it doesn't exist
            side = PositionSide.BUY if self.__direction == Direction.LONG else PositionSide.SELL
            self.__status = PositionStatus(side=side, size=value)
        else:
            self.__status.size = value

    @property
    def liq_price(self):
        if self.__status is None:
            return 0.0
        try:
            return float(self.__status.liquidation_price)
        except (TypeError, ValueError):
            return 0.0

    @property
    def entry_price(self):
        return 0.0 if self.__status is None else self.__status.entry_price
    
    @entry_price.setter
    def entry_price(self, value):
        if self.__status is None:
            # Initialize status if it doesn't exist
            side = PositionSide.BUY if self.__direction == Direction.LONG else PositionSide.SELL
            self.__status = PositionStatus(side=side, size=0.0001, entry_price=value)
        else:
            self.__status.entry_price = value

    @property
    def position_value(self):
        return 0.0 if self.__status is None else float(self.__status.position_value or 0.0)
