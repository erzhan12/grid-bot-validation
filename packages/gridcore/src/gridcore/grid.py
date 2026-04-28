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
from enum import StrEnum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class GridSideType(StrEnum):
    """Grid level side type constants."""
    BUY = 'Buy'
    SELL = 'Sell'
    WAIT = 'Wait'


class Grid:
    """
    Pure grid level calculation logic.

    Manages grid levels (buy/sell/wait zones) for grid trading strategy.
    All calculations are deterministic and produce identical results to
    the original greed.py when given the same inputs.
    """

    def __init__(self, tick_size: Decimal, grid_count: int = 50, grid_step: float = 0.2, rebalance_threshold: float = 0.3,
                 on_change: Optional[Callable[[list[dict]], None]] = None):
        """
        Initialize Grid calculator.

        Args:
            tick_size: Minimum price increment for the symbol (e.g., 0.1 for BTCUSDT)
            grid_count: Number of grid levels (default 50 = 25 buy + 1 wait + 25 sell)
            grid_step: Step size in percentage (default 0.2 = 0.2% between levels)
            rebalance_threshold: Threshold for rebalancing grid when imbalanced (default 0.3 = 30%)
            on_change: Optional callback fired after build_grid/update_grid mutates self.grid.
                Used by callers (e.g. live runner) to persist grid state. Grid stays pure —
                the callback is just a function reference, no I/O knowledge here.
        """
        self.grid: list[dict] = []
        self.tick_size = tick_size
        self.grid_count = grid_count
        self.grid_step = grid_step
        self.REBALANCE_THRESHOLD = rebalance_threshold
        self._original_anchor_price: Optional[float] = None
        self._on_change = on_change

    def _notify_change(self) -> None:
        """Invoke on_change callback. Errors are logged but never propagate —
        persistence failures must not crash strategy logic."""
        if self._on_change is None:
            return
        try:
            self._on_change(self.grid)
        except Exception as e:
            logger.error("Grid on_change callback failed: %s", e, exc_info=True)

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
            'side': GridSideType.WAIT,
            'price': rounded_price
        })

        # Build upper half (SELL orders)
        price = last_close
        for _ in range(half_grid):
            price = self._round_price(price * (1 + step))
            self.grid.append({'side': GridSideType.SELL, 'price': price})

        # Build lower half (BUY orders)
        price = last_close
        for _ in range(half_grid):
            price = self._round_price(price * (1 - step))
            self.grid.insert(0, {'side': GridSideType.BUY, 'price': price})

        # Safety check: Ensure no duplicate prices (critical since grid_level not in hash)
        prices = [g['price'] for g in self.grid]
        unique_prices = set(prices)
        if len(prices) != len(unique_prices):
            # Find duplicates for error message
            from collections import Counter
            duplicates = [price for price, count in Counter(prices).items() if count > 1]
            raise ValueError(
                f"Grid contains duplicate prices: {duplicates}. "
                f"This violates order identity uniqueness (grid_level not in hash). "
                f"Check tick_size={self.tick_size} and grid_step={self.grid_step}."
            )

        self._notify_change()

    def __rebuild_grid(self, last_close: float) -> None:
        """
        Rebuild grid from scratch.

        Reference: bbu2-master/greed.py:43-45
        """
        self.grid = []
        self.build_grid(last_close)

    def restore_grid(self, grid_list: list[dict]) -> bool:
        """
        Restore grid state from a serialized list of {side, price} dicts.

        Validates the restored structure via is_grid_correct(); on failure
        the grid is left empty so the engine will rebuild from market price
        on the first ticker. Does NOT fire on_change — restoration is not a
        mutation worth persisting (we just loaded what was already on disk).

        Args:
            grid_list: Serialized grid (list of {'side': str, 'price': float}).

        Returns:
            True if the grid was restored and validated, False otherwise.
        """
        try:
            restored = [
                {'side': GridSideType(item['side']), 'price': float(item['price'])}
                for item in grid_list
            ]
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Restored grid failed parsing (%s), building fresh grid", e)
            self.grid = []
            return False

        self.grid = restored

        if not self.is_grid_correct():
            logger.warning("Restored grid failed validation, building fresh grid")
            self.grid = []
            return False

        # Derive _original_anchor_price from the WAIT center so anchor_price
        # property continues to reflect a meaningful "center" value.
        wait_indices = [i for i, g in enumerate(self.grid) if g['side'] == GridSideType.WAIT]
        if wait_indices:
            center = (wait_indices[0] + wait_indices[-1]) // 2
        else:
            center = len(self.grid) // 2
        self._original_anchor_price = self.grid[center]['price']

        return True

    @property
    def bounds(self) -> tuple[float, float]:
        """(min_price, max_price) computed in one pass. Raises ValueError if
        the grid is empty. Prefer this over min_grid + max_grid in hot paths
        (e.g. per-tick drift guard) — saves one full iteration over the grid."""
        if not self.grid:
            raise ValueError("Cannot get bounds from empty grid")
        lo = hi = self.grid[0]['price']
        for step in self.grid[1:]:
            p = step['price']
            if p < lo:
                lo = p
            elif p > hi:
                hi = p
        return lo, hi

    @property
    def min_grid(self) -> float:
        """Lowest grid price. Raises ValueError if grid is empty."""
        return self.bounds[0]

    @property
    def max_grid(self) -> float:
        """Highest grid price. Raises ValueError if grid is empty."""
        return self.bounds[1]

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
                grid['side'] = GridSideType.WAIT
            elif last_close < grid['price']:
                grid['side'] = GridSideType.SELL
            elif last_close > grid['price']:
                grid['side'] = GridSideType.BUY

        self.__center_grid()

        self._notify_change()

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
        lowest_buy_price = None
        step = self.grid_step / 100

        # Single pass to count and find prices
        for grid in self.grid:
            if grid['side'] == GridSideType.BUY:
                buy_count += 1
                if lowest_buy_price is None or grid['price'] < lowest_buy_price:
                    lowest_buy_price = grid['price']
            elif grid['side'] == GridSideType.SELL:
                sell_count += 1
                highest_sell_price = grid['price']

        total_count = buy_count + sell_count
        if total_count == 0:
            return

        # Too many buys → shift grid upward
        if (buy_count - sell_count) / total_count > self.REBALANCE_THRESHOLD:
            self.grid.pop(0)  # Delete the bottom line
            price = self._round_price(highest_sell_price * (1 + step))
            self.grid.append({'side': GridSideType.SELL, 'price': price})

        # Too many sells → shift grid downward
        elif (sell_count - buy_count) / total_count > self.REBALANCE_THRESHOLD:
            self.grid.pop()  # Delete the top line
            # Use tracked lowest BUY price; fall back to grid[0] price when
            # no BUY entries remain (e.g. all converted to WAIT after fills).
            base_price = lowest_buy_price if lowest_buy_price is not None else self.grid[0]['price']
            price = self._round_price(base_price * (1 - step))
            self.grid.insert(0, {'side': GridSideType.BUY, 'price': price})

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
        Validate that grid prices are in strictly ascending order (no duplicates).

        Reference: bbu2-master/greed.py:68-77 (commented out validation)

        Returns:
            True if all prices are sorted in strictly ascending order (no duplicates)
        """
        # Initialize with negative infinity to start the comparison
        previous_price = float('-inf')

        for grid in self.grid:
            if grid['price'] <= previous_price:
                return False
            previous_price = grid['price']

        return True

    def is_grid_correct(self) -> bool:
        """
        Validate that grid has correct BUY→WAIT→SELL or BUY→SELL sequence.

        Accepts valid patterns:
        - BUY...BUY → WAIT...WAIT → SELL...SELL: Traditional pattern with WAIT zone (multiple WAITs allowed)
        - BUY...BUY → SELL...SELL: Direct transition from BUY to SELL (no WAIT state)

        Reference: bbu2-master/greed.py:79-104 (commented out validation)

        Returns:
            True if grid follows expected BUY→WAIT→SELL or BUY→SELL pattern
        """
        if not self.__is_price_sorted():
            return False

        # Track which phase we're in: 0=BUY, 1=WAIT, 2=SELL
        current_state = 0
        has_seen_buy = False
        has_seen_sell = False

        for grid in self.grid:
            side = grid['side']

            if side == GridSideType.BUY:
                # BUY is only valid in BUY phase (state 0)
                if current_state == 0:
                    has_seen_buy = True
                    continue
                else:
                    # Can't go back to BUY after WAIT or SELL
                    return False
            elif side == GridSideType.WAIT:
                # WAIT is valid after BUY (state 0) or after other WAITs (state 1)
                if current_state == 0 and has_seen_buy:
                    # Transition from BUY to WAIT phase
                    current_state = 1
                    continue
                elif current_state == 1:
                    # Multiple WAITs in a row are allowed
                    continue
                else:
                    # WAIT not allowed after SELL
                    return False
            elif side == GridSideType.SELL:
                # SELL is valid after BUY (state 0), WAIT (state 1), or other SELLs (state 2)
                if current_state == 0 and has_seen_buy:
                    # Direct transition from BUY to SELL (no WAIT)
                    current_state = 2
                    has_seen_sell = True
                    continue
                elif current_state == 1:
                    # Transition from WAIT to SELL
                    current_state = 2
                    has_seen_sell = True
                    continue
                elif current_state == 2:
                    # Multiple SELLs in a row are allowed
                    continue
                else:
                    return False

        # Grid is correct if we've seen both BUY and SELL, and ended in SELL phase
        return has_seen_buy and has_seen_sell and current_state == 2

    @property
    def __grid_count_sell(self) -> int:
        """
        Count number of SELL levels.

        Reference: bbu2-master/greed.py:143-149
        """
        return sum(1 for step in self.grid if step['side'] == GridSideType.SELL)

    @property
    def __grid_count_buy(self) -> int:
        """
        Count number of BUY levels.

        Reference: bbu2-master/greed.py:151-157
        """
        return sum(1 for step in self.grid if step['side'] == GridSideType.BUY)

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
