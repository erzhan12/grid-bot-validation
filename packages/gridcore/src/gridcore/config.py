"""
Configuration models for grid trading strategy.

This module defines the configuration dataclasses used to parameterize
the grid trading strategy.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GridConfig:
    """
    Configuration for grid trading strategy.

    Attributes:
        greed_count: Number of grid levels (default: 50 means 25 buy + 1 wait + 25 sell)
        greed_step: Step size in percentage (default: 0.2 means 0.2% between levels)
        rebalance_threshold: Threshold for rebalancing grid when imbalanced (default: 0.3 = 30%)
    """
    greed_count: int = 50
    greed_step: float = 0.2
    rebalance_threshold: float = 0.3

    def __post_init__(self):
        """Validate configuration parameters."""
        if self.greed_count <= 0:
            raise ValueError(f"greed_count must be positive, got {self.greed_count}")
        if self.greed_step <= 0:
            raise ValueError(f"greed_step must be positive, got {self.greed_step}")
        if not (0 < self.rebalance_threshold < 1):
            raise ValueError(f"rebalance_threshold must be between 0 and 1, got {self.rebalance_threshold}")
