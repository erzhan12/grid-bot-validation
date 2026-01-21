"""
Tests for GridAnchorStore persistence functionality.
"""

import json
import math
import os
import tempfile
import pytest
from unittest.mock import patch, mock_open, MagicMock

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

    def test_multiple_stores_same_file(self, tmp_path):
        """Test multiple GridAnchorStore instances accessing same file."""
        file_path = str(tmp_path / "grid_anchor.json")

        # First store saves data
        store1 = GridAnchorStore(file_path)
        store1.save("btcusdt_main", 100000.0, 0.2, 50)

        # Second store can read it
        store2 = GridAnchorStore(file_path)
        data = store2.load("btcusdt_main")
        assert data['anchor_price'] == 100000.0

        # Second store updates
        store2.save("ethusdt_main", 3500.0, 0.3, 40)

        # First store can see updates
        eth_data = store1.load("ethusdt_main")
        assert eth_data['anchor_price'] == 3500.0

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


class TestGridAnchorStoreIOErrors:
    """Tests for IOError handling in GridAnchorStore."""

    def test_load_with_permission_error(self, tmp_path):
        """Test load handles permission errors gracefully."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save valid data first
        store.save("btcusdt_main", 100000.0, 0.2, 50)

        # Mock open to raise PermissionError
        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            data = store.load("btcusdt_main")
            assert data is None

    def test_save_with_permission_error(self, tmp_path):
        """Test save handles permission errors when writing."""
        file_path = str(tmp_path / "restricted" / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Mock os.makedirs to succeed but open to fail with PermissionError
        with patch("os.makedirs"):
            with patch("builtins.open", side_effect=PermissionError("Permission denied")):
                # Should raise the PermissionError
                with pytest.raises(PermissionError):
                    store.save("btcusdt_main", 100000.0, 0.2, 50)

    def test_delete_with_read_permission_error(self, tmp_path):
        """Test delete returns False when file cannot be read due to permissions."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save data first
        store.save("btcusdt_main", 100000.0, 0.2, 50)

        # Mock open to raise PermissionError on read
        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            result = store.delete("btcusdt_main")
            assert result is False

    def test_delete_preserves_other_strategies(self, tmp_path):
        """Test delete only removes target strategy, preserves others."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save multiple strategies
        store.save("btcusdt_main", 100000.0, 0.2, 50)
        store.save("ethusdt_main", 3500.0, 0.3, 40)
        store.save("solusdt_main", 150.0, 0.5, 30)

        # Delete one strategy
        result = store.delete("ethusdt_main")
        assert result is True

        # Verify others are preserved
        btc_data = store.load("btcusdt_main")
        assert btc_data is not None
        assert btc_data['anchor_price'] == 100000.0

        sol_data = store.load("solusdt_main")
        assert sol_data is not None
        assert sol_data['anchor_price'] == 150.0

        # Verify deleted strategy is gone
        eth_data = store.load("ethusdt_main")
        assert eth_data is None


class TestGridAnchorStoreCorruption:
    """Tests for handling corrupted data scenarios."""

    def test_delete_with_corrupted_json(self, tmp_path):
        """Test delete returns False when JSON is corrupted."""
        file_path = str(tmp_path / "grid_anchor.json")

        # Write corrupted JSON
        with open(file_path, 'w') as f:
            f.write("not valid json {{{")

        store = GridAnchorStore(file_path)
        result = store.delete("btcusdt_main")
        assert result is False

    def test_save_overwrites_corrupted_data(self, tmp_path):
        """Test save succeeds even when existing file has corrupted JSON."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save initial valid data
        store.save("btcusdt_main", 100000.0, 0.2, 50)

        # Corrupt the file
        with open(file_path, 'w') as f:
            f.write("corrupted json {{{")

        # Save should succeed and overwrite corrupted data
        store.save("ethusdt_main", 3500.0, 0.3, 40)

        # Verify new data is saved
        data = store.load("ethusdt_main")
        assert data is not None
        assert data['anchor_price'] == 3500.0

        # Original data should be lost (corrupted file was overwritten with empty dict)
        btc_data = store.load("btcusdt_main")
        assert btc_data is None


class TestGridAnchorStoreEdgeFilePaths:
    """Tests for edge cases with file paths."""

    def test_save_with_no_parent_directory(self, tmp_path):
        """Test save works with file path that has no parent directory."""
        # Change to tmp_path to test relative path
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            store = GridAnchorStore("grid_anchor.json")

            store.save("btcusdt_main", 100000.0, 0.2, 50)

            # Verify file was created in current directory
            assert os.path.exists("grid_anchor.json")
            data = store.load("btcusdt_main")
            assert data['anchor_price'] == 100000.0
        finally:
            os.chdir(original_cwd)

    def test_empty_file_after_deleting_last_strategy(self, tmp_path):
        """Test that deleting the last strategy leaves an empty JSON object."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Save single strategy
        store.save("btcusdt_main", 100000.0, 0.2, 50)

        # Delete it
        result = store.delete("btcusdt_main")
        assert result is True

        # File should contain empty JSON object
        with open(file_path, 'r') as f:
            data = json.load(f)
        assert data == {}

    def test_very_long_file_path(self, tmp_path):
        """Test save and load work with very long file paths."""
        # Create a deeply nested directory structure
        long_path = tmp_path
        for i in range(10):
            long_path = long_path / f"nested_directory_{i}"

        file_path = str(long_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Should create all directories and save
        store.save("btcusdt_main", 100000.0, 0.2, 50)

        assert os.path.exists(file_path)
        data = store.load("btcusdt_main")
        assert data['anchor_price'] == 100000.0


class TestGridAnchorStoreEdgeValues:
    """Tests for edge case data values."""

    def test_save_with_negative_values(self, tmp_path):
        """Test save works with negative values."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Negative values (may not make sense for trading but should persist)
        store.save("test_strat", -100.0, -0.5, -10)

        data = store.load("test_strat")
        assert data['anchor_price'] == -100.0
        assert data['grid_step'] == -0.5
        assert data['grid_count'] == -10

    def test_save_with_zero_values(self, tmp_path):
        """Test save works with zero values."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        store.save("test_strat", 0.0, 0.0, 0)

        data = store.load("test_strat")
        assert data['anchor_price'] == 0.0
        assert data['grid_step'] == 0.0
        assert data['grid_count'] == 0

    def test_save_with_very_large_numbers(self, tmp_path):
        """Test save works with very large float values."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Very large but valid float values
        large_price = 1e100
        large_step = 1e50
        large_count = 999999999

        store.save("test_strat", large_price, large_step, large_count)

        data = store.load("test_strat")
        assert data['anchor_price'] == large_price 
        assert data['grid_step'] == large_step
        assert data['grid_count'] == large_count

    def test_save_with_nan_values(self, tmp_path):
        """Test save with NaN values (Python json allows NaN by default)."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Python's json.dump() allows NaN by default (writes as JavaScript literal)
        store.save("test_strat", float('nan'), 0.2, 50)

        # Verify it was saved and can be loaded
        data = store.load("test_strat")
        assert data is not None
        # NaN != NaN, so use math.isnan()
        assert math.isnan(data['anchor_price'])
        assert data['grid_step'] == 0.2
        assert data['grid_count'] == 50

    def test_save_with_infinity_values(self, tmp_path):
        """Test save with infinity values (Python json allows Infinity by default)."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Python's json.dump() allows Infinity by default (writes as JavaScript literal)
        store.save("test_strat", float('inf'), 0.2, 50)

        # Verify it was saved and can be loaded
        data = store.load("test_strat")
        assert data is not None
        assert math.isinf(data['anchor_price'])
        assert data['anchor_price'] > 0  # Positive infinity
        assert data['grid_step'] == 0.2
        assert data['grid_count'] == 50

    def test_load_returns_correct_types(self, tmp_path):
        """Test that loaded data has correct Python types."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        store.save("btcusdt_main", 100000.0, 0.2, 50)

        data = store.load("btcusdt_main")
        assert isinstance(data, dict)
        assert isinstance(data['anchor_price'], (int, float))
        assert isinstance(data['grid_step'], (int, float))
        assert isinstance(data['grid_count'], int)


class TestGridAnchorStoreSpecialStratIds:
    """Tests for special characters in strat_id."""

    def test_strat_id_with_spaces(self, tmp_path):
        """Test strat_id with spaces works correctly."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        strat_id = "btc usdt main strategy"
        store.save(strat_id, 100000.0, 0.2, 50)

        data = store.load(strat_id)
        assert data is not None
        assert data['anchor_price'] == 100000.0

    def test_strat_id_with_unicode(self, tmp_path):
        """Test strat_id with unicode characters."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        # Unicode: emoji, Chinese, Arabic
        strat_id = "策略_🚀_استراتيجية"
        store.save(strat_id, 100000.0, 0.2, 50)

        data = store.load(strat_id)
        assert data is not None
        assert data['anchor_price'] == 100000.0

    def test_strat_id_with_special_chars(self, tmp_path):
        """Test strat_id with special characters."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        strat_id = "btc@usdt#main$strategy%^&*()"
        store.save(strat_id, 100000.0, 0.2, 50)

        data = store.load(strat_id)
        assert data is not None
        assert data['anchor_price'] == 100000.0

    def test_empty_strat_id(self, tmp_path):
        """Test empty string as strat_id."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        strat_id = ""
        with pytest.raises(ValueError, match="strat_id must be a non-empty string"):
            store.save(strat_id, 100000.0, 0.2, 50)
        with pytest.raises(ValueError, match="strat_id must be a non-empty string"):
            store.load(strat_id)
        with pytest.raises(ValueError, match="strat_id must be a non-empty string"):
            store.delete(strat_id)


class TestGridAnchorStoreMiscEdgeCases:
    """Tests for miscellaneous edge cases."""

    def test_save_creates_valid_json_format(self, tmp_path):
        """Test that save creates properly formatted JSON with indentation."""
        file_path = str(tmp_path / "grid_anchor.json")
        store = GridAnchorStore(file_path)

        store.save("btcusdt_main", 100000.0, 0.2, 50)

        # Read raw file content
        with open(file_path, 'r') as f:
            content = f.read()

        # Should have indentation (indent=2)
        assert '\n' in content
        assert '  ' in content  # 2-space indent

        # Should be valid JSON
        parsed = json.loads(content)
        assert parsed["btcusdt_main"]["anchor_price"] == 100000.0

    def test_file_path_property_accessible(self):
        """Test that file_path attribute is accessible."""
        store = GridAnchorStore("custom/path/grid_anchor.json")
        assert store.file_path == "custom/path/grid_anchor.json"

        # Should be able to read it
        path = store.file_path
        assert isinstance(path, str)
        assert "grid_anchor.json" in path

    def test_multiple_stores_same_file(self, tmp_path):
        """Test multiple GridAnchorStore instances accessing same file."""
        file_path = str(tmp_path / "grid_anchor.json")

        # First store saves data
        store1 = GridAnchorStore(file_path)
        store1.save("btcusdt_main", 100000.0, 0.2, 50)

        # Second store can read it
        store2 = GridAnchorStore(file_path)
        data = store2.load("btcusdt_main")
        assert data['anchor_price'] == 100000.0

        # Second store updates
        store2.save("ethusdt_main", 3500.0, 0.3, 40)

        # First store can see updates
        eth_data = store1.load("ethusdt_main")
        assert eth_data['anchor_price'] == 3500.0
