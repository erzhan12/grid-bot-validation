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
        grid_count: Number of grid levels (default: 50 means 25 buy + 1 wait + 25 sell)
        grid_step: Step size in percentage (default: 0.2 means 0.2% between levels)
        rebalance_threshold: Threshold for rebalancing grid when imbalanced (default: 0.3 = 30%)
    """
    grid_count: int = 50
    grid_step: float = 0.2
    rebalance_threshold: float = 0.3

    def __post_init__(self):
        """Validate configuration parameters."""
        if self.grid_count <= 0:
            raise ValueError(f"grid_count must be positive, got {self.grid_count}")
        if self.grid_step <= 0:
            raise ValueError(f"grid_step must be positive, got {self.grid_step}")
        if not (0 < self.rebalance_threshold < 1):
            raise ValueError(f"rebalance_threshold must be between 0 and 1, got {self.rebalance_threshold}")
