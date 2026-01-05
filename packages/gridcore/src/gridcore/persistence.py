"""
Grid anchor persistence for maintaining grid levels across restarts.

This module provides file-based persistence for grid anchor data,
keyed by strat_id to support multiple strategy instances.
"""

import json
import os
from typing import Optional


class GridAnchorStore:
    """
    File-based storage for grid anchor data.

    Stores anchor_price, grid_step, and grid_count per strat_id
    to enable grid restoration after restarts.

    Reference: Similar pattern to bbu2-master/db_files.py
    """

    def __init__(self, file_path: str = 'db/grid_anchor.json'):
        """
        Initialize anchor store.

        Args:
            file_path: Path to JSON file for storing anchor data
        """
        self.file_path = file_path

    def load(self, strat_id: str) -> Optional[dict]:
        """
        Load anchor data for a strategy.

        Args:
            strat_id: Strategy identifier

        Returns:
            Dict with anchor_price, grid_step, grid_count or None if not found
        """
        if not os.path.exists(self.file_path):
            return None

        try:
            with open(self.file_path, 'r') as f:
                all_anchors = json.load(f)
            return all_anchors.get(strat_id)
        except (json.JSONDecodeError, IOError):
            return None

    def save(self, strat_id: str, anchor_price: float, grid_step: float, grid_count: int) -> None:
        """
        Save anchor data for a strategy.

        Args:
            strat_id: Strategy identifier
            anchor_price: Center price the grid was built around
            grid_step: Grid step size in percentage
            grid_count: Number of grid levels
        """
        # Load existing data
        all_anchors = {}
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f:
                    all_anchors = json.load(f)
            except (json.JSONDecodeError, IOError):
                all_anchors = {}

        # Update with new data
        all_anchors[strat_id] = {
            'anchor_price': anchor_price,
            'grid_step': grid_step,
            'grid_count': grid_count
        }

        # Ensure directory exists
        dir_path = os.path.dirname(self.file_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path)

        # Write to file
        with open(self.file_path, 'w') as f:
            json.dump(all_anchors, f, indent=2)

    def delete(self, strat_id: str) -> bool:
        """
        Delete anchor data for a strategy.

        Args:
            strat_id: Strategy identifier

        Returns:
            True if deleted, False if not found
        """
        if not os.path.exists(self.file_path):
            return False

        try:
            with open(self.file_path, 'r') as f:
                all_anchors = json.load(f)

            if strat_id not in all_anchors:
                return False

            del all_anchors[strat_id]

            with open(self.file_path, 'w') as f:
                json.dump(all_anchors, f, indent=2)

            return True
        except (json.JSONDecodeError, IOError):
            return False
