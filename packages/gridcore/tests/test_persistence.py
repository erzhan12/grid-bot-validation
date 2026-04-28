"""
Tests for GridStateStore persistence functionality.
"""

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from gridcore.persistence import GridStateStore


def _sample_grid() -> list[dict]:
    return [
        {"side": "Buy", "price": 100.0},
        {"side": "Buy", "price": 101.0},
        {"side": "Wait", "price": 102.0},
        {"side": "Sell", "price": 103.0},
        {"side": "Sell", "price": 104.0},
    ]


class TestGridStateStoreRoundtrip:
    def test_save_and_load_roundtrip(self, tmp_path):
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        grid = _sample_grid()

        store.save(strat_id="strat1", grid=grid, grid_step=0.2, grid_count=20)
        store.flush()

        loaded = store.load("strat1")
        assert loaded is not None
        assert loaded["grid"] == grid
        assert loaded["grid_step"] == 0.2
        assert loaded["grid_count"] == 20

    def test_save_overwrites_existing_entry(self, tmp_path):
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        store.save("strat1", _sample_grid(), grid_step=0.2, grid_count=20)

        new_grid = [{"side": "Wait", "price": 200.0}, {"side": "Sell", "price": 201.0}]
        store.save("strat1", new_grid, grid_step=0.3, grid_count=10)
        store.flush()

        loaded = store.load("strat1")
        assert loaded["grid"] == new_grid
        assert loaded["grid_step"] == 0.3
        assert loaded["grid_count"] == 10

    def test_multiple_strats_share_file(self, tmp_path):
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)

        grid1 = _sample_grid()
        grid2 = [{"side": "Buy", "price": 50.0}, {"side": "Wait", "price": 51.0}]

        store.save("strat_a", grid1, grid_step=0.2, grid_count=20)
        store.save("strat_b", grid2, grid_step=0.5, grid_count=10)
        store.flush()

        assert store.load("strat_a")["grid"] == grid1
        assert store.load("strat_b")["grid"] == grid2

    def test_load_missing_file_returns_none(self, tmp_path):
        file_path = str(tmp_path / "missing.json")
        store = GridStateStore(file_path)
        assert store.load("strat1") is None

    def test_load_unknown_strat_id_returns_none(self, tmp_path):
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        store.save("strat1", _sample_grid(), grid_step=0.2, grid_count=20)
        store.flush()
        assert store.load("strat_other") is None

    def test_load_empty_strat_id_raises(self, tmp_path):
        store = GridStateStore(str(tmp_path / "grid_state.json"))
        with pytest.raises(ValueError):
            store.load("")

    def test_save_empty_strat_id_raises(self, tmp_path):
        store = GridStateStore(str(tmp_path / "grid_state.json"))
        with pytest.raises(ValueError):
            store.save("", _sample_grid(), 0.2, 20)


class TestLegacyDetection:
    def test_legacy_anchor_format_returns_none(self, tmp_path, caplog):
        """Legacy entry (no `grid` key) is treated as no saved state."""
        file_path = str(tmp_path / "grid_state.json")
        legacy_data = {
            "strat1": {
                "anchor_price": 100.0,
                "grid_step": 0.2,
                "grid_count": 20,
            }
        }
        Path(file_path).write_text(json.dumps(legacy_data))

        store = GridStateStore(file_path)
        with caplog.at_level("INFO"):
            result = store.load("strat1")

        assert result is None
        assert any("Legacy anchor format ignored" in r.message for r in caplog.records)

    def test_new_format_loads_normally(self, tmp_path):
        """Entries with `grid` key load normally — does not trigger legacy path."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        store.save("strat1", _sample_grid(), grid_step=0.2, grid_count=20)
        store.flush()

        loaded = store.load("strat1")
        assert loaded is not None
        assert "grid" in loaded


class TestCorruption:
    def test_corrupt_json_returns_none(self, tmp_path):
        file_path = str(tmp_path / "grid_state.json")
        Path(file_path).write_text("not json at all")

        store = GridStateStore(file_path)
        assert store.load("strat1") is None

    def test_partial_json_returns_none(self, tmp_path):
        file_path = str(tmp_path / "grid_state.json")
        Path(file_path).write_text('{"strat1": {"grid":')

        store = GridStateStore(file_path)
        assert store.load("strat1") is None

    def test_non_dict_entry_returns_none(self, tmp_path):
        """Hand-edited file with a non-dict entry must not crash load() —
        regression for `'grid' not in entry` raising TypeError on int/str/list."""
        file_path = str(tmp_path / "grid_state.json")
        Path(file_path).write_text(json.dumps({
            "int_entry": 1,
            "str_entry": "garbage",
            "list_entry": [1, 2, 3],
        }))

        store = GridStateStore(file_path)
        assert store.load("int_entry") is None
        assert store.load("str_entry") is None
        assert store.load("list_entry") is None

    @pytest.mark.parametrize("root_value", ["[]", '"x"', "1", "true", "null"])
    def test_non_dict_root_does_not_crash_load(self, tmp_path, root_value):
        """Hand-edited file with valid JSON but non-object root (e.g. `[]`)
        must not crash load() — regression for AttributeError on
        `all_data.get(...)` when root is a list/string/number."""
        file_path = str(tmp_path / "grid_state.json")
        Path(file_path).write_text(root_value)

        store = GridStateStore(file_path)
        assert store.load("any_strat") is None

    def test_save_self_heals_non_dict_root(self, tmp_path):
        """A file whose root was hand-edited to a non-dict must be silently
        overwritten on the next save(), not crash with TypeError. Persistence
        layer should be self-healing without manual file cleanup."""
        file_path = str(tmp_path / "grid_state.json")
        Path(file_path).write_text("[]")

        store = GridStateStore(file_path)
        grid = _sample_grid()
        store.save("strat1", grid, 0.2, 20)
        store.flush()

        loaded = store.load("strat1")
        assert loaded is not None
        assert loaded["grid"] == grid

    def test_delete_non_dict_root_returns_false(self, tmp_path):
        """delete() on a file with non-dict root must not crash — returns
        False (nothing to delete) since the per-strat entry can't exist."""
        file_path = str(tmp_path / "grid_state.json")
        Path(file_path).write_text("[]")

        store = GridStateStore(file_path)
        assert store.delete("strat1") is False

    def test_save_recovers_from_corrupt_existing_file(self, tmp_path):
        """Corrupt existing file is silently overwritten on save (matches legacy behavior)."""
        file_path = str(tmp_path / "grid_state.json")
        Path(file_path).write_text("garbage")

        store = GridStateStore(file_path)
        grid = _sample_grid()
        store.save("strat1", grid, grid_step=0.2, grid_count=20)
        store.flush()

        loaded = store.load("strat1")
        assert loaded["grid"] == grid


class TestAtomicWrite:
    def test_old_file_survives_failed_write(self, tmp_path):
        """If os.replace fails after the tmp file is written, the original file
        must remain intact (atomicity guarantee)."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)

        # First write succeeds.
        original_grid = _sample_grid()
        store.save("strat1", original_grid, grid_step=0.2, grid_count=20)
        store.flush()
        original_content = Path(file_path).read_text()

        # Second write fails at os.replace step.
        new_grid = [{"side": "Wait", "price": 999.0}]
        with patch("os.replace", side_effect=OSError("simulated failure")):
            with pytest.raises(OSError):
                store._sync_write_to_disk(
                    "strat1",
                    {"grid": new_grid, "grid_step": 0.2, "grid_count": 20},
                )

        # Original file is untouched.
        assert Path(file_path).read_text() == original_content
        assert os.path.exists(file_path)


class TestDedupe:
    def test_identical_payload_skips_thread_spawn(self, tmp_path):
        """Two identical save() calls — the second must short-circuit before
        even spawning a background thread (no work, no deepcopy)."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        grid = _sample_grid()

        store.save("strat1", grid, grid_step=0.2, grid_count=20)
        store.flush()

        with patch("threading.Thread") as mock_thread:
            store.save("strat1", grid, grid_step=0.2, grid_count=20)

        mock_thread.assert_not_called()

    def test_changed_payload_triggers_write(self, tmp_path):
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        grid_a = _sample_grid()
        grid_b = list(grid_a) + [{"side": "Sell", "price": 105.0}]

        store.save("strat1", grid_a, grid_step=0.2, grid_count=20)
        store.save("strat1", grid_b, grid_step=0.2, grid_count=20)
        store.flush()

        loaded = store.load("strat1")
        assert loaded["grid"] == grid_b

    def test_dedupe_baseline_unaffected_by_in_place_mutation(self, tmp_path):
        """The store fingerprints by structural identity at save() time. If
        the caller later mutates the same list in place, the next save() must
        still detect the difference and trigger a write — i.e. the fingerprint
        must reflect the snapshot at the moment of the prior save, not a live
        reference."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        grid = _sample_grid()

        store.save("strat1", grid, grid_step=0.2, grid_count=20)
        store.flush()

        grid[0]["side"] = "Wait"  # In-place mutation by caller.

        with patch("threading.Thread") as mock_thread:
            store.save("strat1", grid, grid_step=0.2, grid_count=20)
            assert mock_thread.call_count == 1


class TestFlush:
    def test_flush_waits_for_pending_writes(self, tmp_path):
        """flush() must block until all in-flight writes have completed —
        used by tests and graceful shutdown."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        store.save("strat1", _sample_grid(), grid_step=0.2, grid_count=20)
        store.flush()
        # File exists immediately after flush returns.
        assert Path(file_path).exists()
        loaded = store.load("strat1")
        assert loaded is not None

    def test_flush_with_no_pending_writes_is_noop(self, tmp_path):
        store = GridStateStore(str(tmp_path / "grid_state.json"))
        store.flush()  # Should not raise.


class TestBackgroundWrite:
    def test_save_does_not_block_caller(self, tmp_path):
        """save() returns immediately; disk I/O happens in a background thread."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        store.save("strat1", _sample_grid(), grid_step=0.2, grid_count=20)
        # If save() were blocking on fsync, this assertion would race with
        # the actual disk write. flush() makes the test deterministic.
        store.flush()
        assert store.load("strat1") is not None

    def test_concurrent_writes_serialized_by_lock(self, tmp_path):
        """Two strats writing back-to-back both end up on disk (the lock
        serializes; nothing is dropped)."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)

        store.save("a", [{"side": "Wait", "price": 1.0}, {"side": "Sell", "price": 2.0}], 0.2, 10)
        store.save("b", [{"side": "Buy", "price": 3.0}, {"side": "Wait", "price": 4.0}], 0.2, 10)
        store.flush()

        assert store.load("a") is not None
        assert store.load("b") is not None

    def test_burst_saves_persist_latest_payload(self, tmp_path):
        """Five rapid distinct saves for the same strat_id must end with the
        last payload on disk — verifies the single-writer-per-strat coalescing
        does not reorder writes (the original threading.Lock approach was not
        FIFO and could write older payloads after newer ones)."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)

        for i in range(5):
            grid = [{"side": "Wait", "price": 100.0 + i}, {"side": "Sell", "price": 200.0 + i}]
            store.save("strat1", grid, 0.2, 10)
        store.flush()

        loaded = store.load("strat1")
        # Last payload (i=4) must win.
        assert loaded["grid"][0]["price"] == 104.0
        assert loaded["grid"][1]["price"] == 204.0

    def test_save_during_in_flight_write_is_coalesced(self, tmp_path):
        """A save() that arrives while an earlier write is in flight must NOT
        spawn a second writer thread — the in-flight writer drains the slot."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)

        spawn_count = 0
        original_thread = threading.Thread

        def counting_thread(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return original_thread(*args, **kwargs)

        with patch("threading.Thread", side_effect=counting_thread):
            store.save("strat1", _sample_grid(), 0.2, 20)
            grid_b = _sample_grid() + [{"side": "Sell", "price": 999.0}]
            store.save("strat1", grid_b, 0.2, 20)

        store.flush()
        # At most 2 thread spawns (in the worst case the first finished before
        # the second save), but importantly the disk has the latest payload.
        assert spawn_count <= 2
        assert store.load("strat1")["grid"] == grid_b

    def test_write_failure_logged_not_raised(self, tmp_path, caplog):
        """A disk-write failure inside the background thread is logged but
        never propagates — persistence failures must not crash the bot."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)

        with patch.object(store, "_sync_write_to_disk", side_effect=OSError("boom")):
            with caplog.at_level("ERROR"):
                store.save("strat1", _sample_grid(), 0.2, 20)
                store.flush()

        assert any("Save failed for strat1" in r.message for r in caplog.records)

    def test_failed_write_allows_retry_with_same_payload(self, tmp_path):
        """After a transient write failure the same payload must persist on
        the next save() — regression for stale-dedupe (the failed payload's
        fingerprint stayed in _last_fingerprint and silently skipped retries
        until the grid happened to mutate to a different fingerprint)."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        grid = _sample_grid()

        # First save fails at the disk layer.
        with patch.object(store, "_sync_write_to_disk", side_effect=OSError("transient")):
            store.save("strat1", grid, 0.2, 20)
            store.flush()
        assert store.load("strat1") is None  # nothing on disk

        # Retry with the SAME payload — must reach disk now that the writer
        # rolled back the dedupe fingerprint on failure.
        store.save("strat1", grid, 0.2, 20)
        store.flush()

        loaded = store.load("strat1")
        assert loaded is not None
        assert loaded["grid"] == grid

    def test_failed_write_does_not_overwrite_newer_payload_dedupe(self, tmp_path):
        """If a newer payload was enqueued between a save() and its writer's
        failure, the rollback must NOT clear the newer payload's fingerprint
        (it's still valid and pending)."""
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        grid_a = _sample_grid()
        grid_b = list(grid_a) + [{"side": "Sell", "price": 999.0}]

        # Both writes fail. After flush, neither is on disk and dedupe is
        # cleared so a retry of either succeeds.
        with patch.object(store, "_sync_write_to_disk", side_effect=OSError("boom")):
            store.save("strat1", grid_a, 0.2, 20)
            store.save("strat1", grid_b, 0.2, 20)
            store.flush()

        # Without the rollback, the second save's fingerprint would persist
        # in _last_fingerprint even after the failure, and a retry would
        # silently skip. Verify retry works.
        store.save("strat1", grid_b, 0.2, 20)
        store.flush()
        loaded = store.load("strat1")
        assert loaded is not None
        assert loaded["grid"] == grid_b


class TestDelete:
    def test_delete_existing_strat(self, tmp_path):
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        store.save("strat1", _sample_grid(), 0.2, 20)
        store.flush()

        assert store.delete("strat1") is True
        assert store.load("strat1") is None

    def test_delete_unknown_strat_returns_false(self, tmp_path):
        file_path = str(tmp_path / "grid_state.json")
        store = GridStateStore(file_path)
        store.save("strat1", _sample_grid(), 0.2, 20)
        store.flush()
        assert store.delete("strat_other") is False

    def test_delete_missing_file_returns_false(self, tmp_path):
        store = GridStateStore(str(tmp_path / "missing.json"))
        assert store.delete("strat1") is False
