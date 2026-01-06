"""
Grid level calculations for grid trading strategy.

Extracted from bbu2-master/greed.py with the following key transformations:
- Removed BybitApiUsdt.round_price() dependency → implemented internal _round_price()
- Removed database calls (read_from_db(), write_to_db())
- Pass tick_size as parameter instead of from BybitApiUsdt
- Added validation methods (__is_price_sorted(), is_greed_correct())
- Removed strat dependency, made self-contained
"""

import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


class Grid:
    """
    Pure grid level calculation logic.

    Manages grid levels (buy/sell/wait zones) for grid trading strategy.
    All calculations are deterministic and produce identical results to
    the original greed.py when given the same inputs.
    """

    def __init__(self, tick_size: Decimal, grid_count: int = 50, grid_step: float = 0.2, rebalance_threshold: float = 0.3):
        """
        Initialize Grid calculator.

        Args:
            tick_size: Minimum price increment for the symbol (e.g., 0.1 for BTCUSDT)
            grid_count: Number of grid levels (default 50 = 25 buy + 1 wait + 25 sell)
            grid_step: Step size in percentage (default 0.2 = 0.2% between levels)
            rebalance_threshold: Threshold for rebalancing grid when imbalanced (default 0.3 = 30%)
        """
        self.grid: list[dict] = []
        self.tick_size = tick_size
        self.grid_count = grid_count
        self.grid_step = grid_step
        self.BUY = 'Buy'
        self.SELL = 'Sell'
        self.WAIT = 'wait'
        self.REBALANCE_THRESHOLD = rebalance_threshold
        self._original_anchor_price: Optional[float] = None

    def _round_price(self, price: float) -> float:
        """
        Round price to tick_size precision.

        Replaces BybitApiUsdt.round_price() from original code.

        Args:
            price: Raw price to round

        Returns:
            Price rounded to tick_size precision
        """
        tick_size_float = float(self.tick_size)
        rounded = round(price / tick_size_float) * tick_size_float
        # Format to avoid floating-point artifacts
        return float(f'{rounded:.10f}')

    def build_grid(self, last_close: float) -> None:
        """
        Build initial grid centered on last_close price.

        Creates grid_count grid levels:
        - Bottom half: BUY orders
        - Middle: WAIT zone (no orders)
        - Top half: SELL orders

        Reference: bbu2-master/greed.py:18-41

        Args:
            last_close: Current market price to center grid around
        """
        if not last_close:
            return

        # Clear existing grid before building (prevents doubling on rebuild)
        self.grid = []

        half_grid = self.grid_count // 2
        step = self.grid_step / 100

        # Middle line = actual price (WAIT zone)
        # Store the original anchor price for persistence
        rounded_price = self._round_price(last_close)
        self._original_anchor_price = rounded_price
        self.grid.append({
            'side': self.WAIT,
            'price': rounded_price
        })

        # Build upper half (SELL orders)
        price = last_close
        for _ in range(half_grid):
            price = self._round_price(price * (1 + step))
            self.grid.append({'side': self.SELL, 'price': price})

        # Build lower half (BUY orders)
        price = last_close
        for _ in range(half_grid):
            price = self._round_price(price * (1 - step))
            self.grid.insert(0, {'side': self.BUY, 'price': price})

    def __rebuild_grid(self, last_close: float) -> None:
        """
        Rebuild grid from scratch.

        Reference: bbu2-master/greed.py:43-45
        """
        self.grid = []
        self.build_grid(last_close)

    def update_grid(self, last_filled_price: Optional[float], last_close: Optional[float]) -> None:
        """
        Update grid after an order fill.

        Updates grid sides based on:
        1. Mark filled price as WAIT (too close to trade again)
        2. Update BUY/SELL sides based on current price
        3. Rebalance grid if needed

        Reference: bbu2-master/greed.py:48-66

        Args:
            last_filled_price: Price of most recent fill
            last_close: Current market price
        """
        if last_filled_price is None:
            return
        if last_close is None:
            return

        # Rebuild if grid is empty or price moved outside grid bounds
        if not self.grid or not (self.__min_grid < last_close < self.__max_grid):
            logger.info('Rebuild grid: Out of bounds (price=%s)', last_close)
            self.__rebuild_grid(last_close)
            # Continue to apply side assignment logic after rebuild (matches original behavior)

        # Update grid sides
        for grid in self.grid:
            if self.__is_too_close(grid['price'], last_filled_price):
                grid['side'] = self.WAIT
            elif last_close < grid['price']:
                grid['side'] = self.SELL
            elif last_close > grid['price']:
                grid['side'] = self.BUY

        self.__center_grid()

    def __center_grid(self) -> None:
        """
        Rebalance grid if buy/sell ratio becomes too imbalanced.

        If >30% more buys than sells: shift grid up (remove bottom buy, add top sell)
        If >30% more sells than buys: shift grid down (remove top sell, add bottom buy)

        Reference: bbu2-master/greed.py:106-132
        """
        buy_count = 0
        sell_count = 0
        highest_sell_price = 0
        lowest_buy_price = self.grid[0]['price'] if self.grid else 0
        step = self.grid_step / 100

        # Single pass to count and find prices
        for grid in self.grid:
            if grid['side'] == self.BUY:
                buy_count += 1
            elif grid['side'] == self.SELL:
                sell_count += 1
                highest_sell_price = grid['price']

        total_count = buy_count + sell_count
        if total_count == 0:
            return

        # Too many buys → shift grid upward
        if (buy_count - sell_count) / total_count > self.REBALANCE_THRESHOLD:
            self.grid.pop(0)  # Delete the bottom line
            price = self._round_price(highest_sell_price * (1 + step))
            self.grid.append({'side': self.SELL, 'price': price})

        # Too many sells → shift grid downward
        elif (sell_count - buy_count) / total_count > self.REBALANCE_THRESHOLD:
            self.grid.pop()  # Delete the top line
            price = self._round_price(lowest_buy_price * (1 - step))
            self.grid.insert(0, {'side': self.BUY, 'price': price})

    def __is_too_close(self, price1: float, price2: float) -> bool:
        """
        Check if two prices are too close to place orders between them.

        Reference: bbu2-master/greed.py:134-135

        Args:
            price1: First price
            price2: Second price

        Returns:
            True if prices are within grid_step/4 of each other
        """
        return abs(price1 - price2) / price1 * 100 < self.grid_step / 4

    def __is_price_sorted(self) -> bool:
        """
        Validate that grid prices are in ascending order.

        Reference: bbu2-master/greed.py:68-77 (commented out validation)

        Returns:
            True if all prices are sorted ascending
        """
        # Initialize with negative infinity to start the comparison
        previous_price = float('-inf')

        for grid in self.grid:
            if grid['price'] < previous_price:
                return False
            previous_price = grid['price']

        return True

    def is_grid_correct(self) -> bool:
        """
        Validate that grid has correct BUY→WAIT→SELL sequence.

        Reference: bbu2-master/greed.py:79-104 (commented out validation)

        Returns:
            True if grid follows expected BUY→WAIT→SELL pattern
        """
        expected_sequence = [self.BUY, self.WAIT, self.SELL]
        current_state = 0

        if not self.__is_price_sorted():
            return False

        for grid in self.grid:
            side = grid['side']

            # Check if side matches the expected state
            if side == expected_sequence[current_state]:
                continue
            # If it encounters a 'wait', move to the 'Sell' state
            elif side == self.WAIT and current_state == 0:
                current_state = 2
            # If a 'Sell' is found after 'Buy', it should have found a 'wait' first
            elif side == self.SELL and current_state == 1:
                current_state = 2
            else:
                # If the sequence is not in order, return False
                return False

        # Check if 'Sell' was the last state to confirm the sequence completed correctly
        return current_state == 2

    @property
    def __grid_count_sell(self) -> int:
        """
        Count number of SELL levels.

        Reference: bbu2-master/greed.py:143-149
        """
        return sum(1 for step in self.grid if step['side'] == self.SELL)

    @property
    def __grid_count_buy(self) -> int:
        """
        Count number of BUY levels.

        Reference: bbu2-master/greed.py:151-157
        """
        return sum(1 for step in self.grid if step['side'] == self.BUY)

    @property
    def __min_grid(self) -> float:
        """
        Get minimum grid price.

        Reference: bbu2-master/greed.py:159-162

        Raises:
            ValueError: If grid is empty (should not happen if called after empty check)
        """
        if not self.grid:
            raise ValueError("Cannot get min_grid from empty grid")
        prices = [step['price'] for step in self.grid]
        return min(prices)

    @property
    def __max_grid(self) -> float:
        """
        Get maximum grid price.

        Reference: bbu2-master/greed.py:164-167

        Raises:
            ValueError: If grid is empty (should not happen if called after empty check)
        """
        if not self.grid:
            raise ValueError("Cannot get max_grid from empty grid")
        prices = [step['price'] for step in self.grid]
        return max(prices)

    @property
    def anchor_price(self) -> float | None:
        """
        Get anchor price (WAIT zone center price).

        The anchor price is the price around which the grid was built.
        This is the original center WAIT zone price, not any WAIT zones
        created after order fills.

        Returns:
            The original WAIT zone center price, or None if grid was never built
        """
        return self._original_anchor_price
