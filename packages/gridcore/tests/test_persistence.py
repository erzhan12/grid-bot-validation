"""
Tests for GridAnchorStore persistence functionality.
"""

import json
import os
import tempfile
import pytest

from gridcore.persistence import GridAnchorStore


class TestGridAnchorStore:
    """Tests for GridAnchorStore."""

    def test_save_and_load(self, tmp_path):
        """Test basic save and load functionality."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save anchor data
        store.save(
            strat_id="btcusdt_main",
            anchor_price=100000.0,
            grid_step=0.2,
            grid_count=50
        )

        # Load and verify
        data = store.load("btcusdt_main")
        assert data is not None
        assert data['anchor_price'] == 100000.0
        assert data['grid_step'] == 0.2
        assert data['grid_count'] == 50

    def test_load_nonexistent_strat_id(self, tmp_path):
        """Test loading non-existent strat_id returns None."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save one strat
        store.save("btcusdt_main", 100000.0, 0.2, 50)

        # Try to load different strat
        data = store.load("ethusdt_main")
        assert data is None

    def test_load_nonexistent_file(self, tmp_path):
        """Test loading from non-existent file returns None."""
        file_path = str(tmp_path / "nonexistent.json")
        store = GridAnchorStore(file_path)

        data = store.load("btcusdt_main")
        assert data is None

    def test_multiple_strategies(self, tmp_path):
        """Test saving and loading multiple strategies."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save multiple strategies
        store.save("btcusdt_main", 100000.0, 0.2, 50)
        store.save("ethusdt_main", 3500.0, 0.3, 40)
        store.save("solusdt_main", 150.0, 0.5, 30)

        # Load and verify each
        btc_data = store.load("btcusdt_main")
        assert btc_data['anchor_price'] == 100000.0
        assert btc_data['grid_step'] == 0.2
        assert btc_data['grid_count'] == 50

        eth_data = store.load("ethusdt_main")
        assert eth_data['anchor_price'] == 3500.0
        assert eth_data['grid_step'] == 0.3
        assert eth_data['grid_count'] == 40

        sol_data = store.load("solusdt_main")
        assert sol_data['anchor_price'] == 150.0
        assert sol_data['grid_step'] == 0.5
        assert sol_data['grid_count'] == 30

    def test_update_existing_strategy(self, tmp_path):
        """Test updating existing strategy overwrites old data."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save initial data
        store.save("btcusdt_main", 100000.0, 0.2, 50)

        # Update with new data
        store.save("btcusdt_main", 105000.0, 0.25, 60)

        # Verify updated data
        data = store.load("btcusdt_main")
        assert data['anchor_price'] == 105000.0
        assert data['grid_step'] == 0.25
        assert data['grid_count'] == 60

    def test_delete_strategy(self, tmp_path):
        """Test deleting a strategy."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save and then delete
        store.save("btcusdt_main", 100000.0, 0.2, 50)
        result = store.delete("btcusdt_main")

        assert result is True
        assert store.load("btcusdt_main") is None

    def test_delete_nonexistent_strategy(self, tmp_path):
        """Test deleting non-existent strategy returns False."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save one strategy
        store.save("btcusdt_main", 100000.0, 0.2, 50)

        # Try to delete different strategy
        result = store.delete("ethusdt_main")
        assert result is False

    def test_delete_from_nonexistent_file(self, tmp_path):
        """Test deleting from non-existent file returns False."""
        file_path = str(tmp_path / "nonexistent.json")
        store = GridAnchorStore(file_path)

        result = store.delete("btcusdt_main")
        assert result is False

    def test_creates_directory_if_not_exists(self, tmp_path):
        """Test that save creates parent directories if they don't exist."""
        file_path = str(tmp_path / "nested" / "dir" / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        store.save("btcusdt_main", 100000.0, 0.2, 50)

        # Verify file was created
        assert os.path.exists(file_path)
        data = store.load("btcusdt_main")
        assert data['anchor_price'] == 100000.0

    def test_handles_corrupted_json(self, tmp_path):
        """Test that load handles corrupted JSON gracefully."""
        file_path = str(tmp_path / "grid_anchor.json")

        # Write corrupted JSON
        with open(file_path, 'w') as f:
            f.write("not valid json {{{")

        store = GridAnchorStore(file_path)
        data = store.load("btcusdt_main")
        assert data is None

    def test_json_format(self, tmp_path):
        """Test that saved JSON has expected format."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        store.save("btcusdt_main", 100000.0, 0.2, 50)
        store.save("ethusdt_main", 3500.0, 0.3, 40)

        # Read raw JSON and verify structure
        with open(file_path, 'r') as f:
            raw_data = json.load(f)

        assert "btcusdt_main" in raw_data
        assert "ethusdt_main" in raw_data
        assert raw_data["btcusdt_main"]["anchor_price"] == 100000.0
        assert raw_data["ethusdt_main"]["grid_step"] == 0.3

    def test_default_file_path(self):
        """Test default file path is db/grid_anchor.json."""
        store = GridAnchorStore()
        assert store.file_path == 'db/grid_anchor.json'
