"""Tests for cache_lock.py including Windows-specific locking behavior.

Windows tests use unittest.mock to simulate msvcrt.locking without
requiring an actual Windows environment, enabling cross-platform CI.
"""

import os
import threading
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from backtest.cache_lock import (
    _LOCK_REGION_BYTES,
    _IN_PROCESS_LOCKS,
    _IN_PROCESS_LOCKS_GUARD,
    acquire_in_process_lock,
    release_in_process_lock,
    acquire_file_lock,
    release_file_lock,
    open_lock_file,
)


# ---------------------------------------------------------------------------
# In-process lock registry
# ---------------------------------------------------------------------------

class TestInProcessLockRegistry:
    """Tests for the ref-counted in-process lock management."""

    def test_acquire_creates_new_entry(self):
        key, lock = acquire_in_process_lock(Path("/tmp/test_new_entry_lock"))
        try:
            assert key in _IN_PROCESS_LOCKS
            assert _IN_PROCESS_LOCKS[key][1] >= 1
        finally:
            release_in_process_lock(key)

    def test_acquire_increments_refcount(self):
        path = Path("/tmp/test_refcount_lock")
        key1, lock1 = acquire_in_process_lock(path)
        key2, lock2 = acquire_in_process_lock(path)
        try:
            assert key1 == key2
            assert lock1 is lock2
            assert _IN_PROCESS_LOCKS[key1][1] >= 2
        finally:
            release_in_process_lock(key1)
            release_in_process_lock(key2)

    def test_release_decrements_refcount(self):
        path = Path("/tmp/test_release_lock")
        key1, _ = acquire_in_process_lock(path)
        key2, _ = acquire_in_process_lock(path)
        initial_count = _IN_PROCESS_LOCKS[key1][1]
        release_in_process_lock(key1)
        assert _IN_PROCESS_LOCKS[key1][1] == initial_count - 1
        release_in_process_lock(key2)

    def test_release_removes_entry_at_zero(self):
        path = Path("/tmp/test_remove_lock")
        key, _ = acquire_in_process_lock(path)
        release_in_process_lock(key)
        assert key not in _IN_PROCESS_LOCKS

    def test_release_nonexistent_key_is_noop(self):
        """Releasing a key that doesn't exist should not raise."""
        release_in_process_lock("/tmp/nonexistent_key")


# ---------------------------------------------------------------------------
# Windows msvcrt locking (mocked)
# ---------------------------------------------------------------------------

class TestWindowsFileLocking:
    """Tests for Windows-specific msvcrt.locking behavior via mocks."""

    def _make_mock_file(self, size: int = 0) -> MagicMock:
        """Create a mock file object that tracks seek position and size."""
        mock_file = MagicMock()
        mock_file.fileno.return_value = 42
        # Track position for seek/tell
        pos = [0]
        file_size = [size]

        def seek_fn(offset, whence=0):
            if whence == os.SEEK_END:
                pos[0] = file_size[0]
            elif whence == 0:
                pos[0] = offset

        def tell_fn():
            return pos[0]

        def write_fn(data):
            file_size[0] += len(data)

        mock_file.seek.side_effect = seek_fn
        mock_file.tell.side_effect = tell_fn
        mock_file.write.side_effect = write_fn
        return mock_file

    @patch("os.name", "nt")
    def test_acquire_lock_pads_small_file(self):
        """File smaller than _LOCK_REGION_BYTES is padded before locking."""
        mock_msvcrt = MagicMock()
        mock_file = self._make_mock_file(size=100)

        with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
            # Re-import to pick up the mock
            import importlib
            import backtest.cache_lock as cl
            importlib.reload(cl)

            cl.acquire_file_lock(mock_file)

        # Should have written padding bytes
        mock_file.write.assert_called_once()
        written_data = mock_file.write.call_args[0][0]
        assert len(written_data) == _LOCK_REGION_BYTES - 100
        # Should have flushed after write
        mock_file.flush.assert_called_once()
        # Should have called msvcrt.locking with LK_LOCK
        mock_msvcrt.locking.assert_called_once_with(42, mock_msvcrt.LK_LOCK, _LOCK_REGION_BYTES)

    @patch("os.name", "nt")
    def test_acquire_lock_no_padding_for_large_file(self):
        """File already >= _LOCK_REGION_BYTES is not padded."""
        mock_msvcrt = MagicMock()
        mock_file = self._make_mock_file(size=_LOCK_REGION_BYTES + 500)

        with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
            import importlib
            import backtest.cache_lock as cl
            importlib.reload(cl)

            cl.acquire_file_lock(mock_file)

        mock_file.write.assert_not_called()
        mock_msvcrt.locking.assert_called_once_with(42, mock_msvcrt.LK_LOCK, _LOCK_REGION_BYTES)

    @patch("os.name", "nt")
    def test_release_lock_seeks_to_zero(self):
        """Release seeks to position 0 before calling msvcrt.locking(LK_UNLCK)."""
        mock_msvcrt = MagicMock()
        mock_file = self._make_mock_file(size=_LOCK_REGION_BYTES)

        with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
            import importlib
            import backtest.cache_lock as cl
            importlib.reload(cl)

            cl.release_file_lock(mock_file)

        mock_file.seek.assert_called_with(0)
        mock_msvcrt.locking.assert_called_once_with(42, mock_msvcrt.LK_UNLCK, _LOCK_REGION_BYTES)

    @patch("os.name", "nt")
    def test_lock_region_size_matches_between_acquire_release(self):
        """Acquire and release use the same _LOCK_REGION_BYTES value."""
        mock_msvcrt = MagicMock()
        mock_file = self._make_mock_file(size=_LOCK_REGION_BYTES)

        with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
            import importlib
            import backtest.cache_lock as cl
            importlib.reload(cl)

            cl.acquire_file_lock(mock_file)
            cl.release_file_lock(mock_file)

        acquire_call = mock_msvcrt.locking.call_args_list[0]
        release_call = mock_msvcrt.locking.call_args_list[1]
        assert acquire_call[0][2] == _LOCK_REGION_BYTES
        assert release_call[0][2] == _LOCK_REGION_BYTES


# ---------------------------------------------------------------------------
# Unix file locking (real fcntl, integration)
# ---------------------------------------------------------------------------

class TestUnixFileLocking:
    """Tests for Unix flock-based file locking."""

    @pytest.mark.skipif(os.name == "nt", reason="Unix only")
    def test_acquire_and_release_no_error(self, tmp_path):
        """Acquire and release cycle completes without error."""
        lock_path = tmp_path / "test.lock"
        lock_file = open_lock_file(lock_path)
        try:
            acquire_file_lock(lock_file)
            release_file_lock(lock_file)
        finally:
            lock_file.close()

    @pytest.mark.skipif(os.name == "nt", reason="Unix only")
    def test_concurrent_lock_serializes_access(self, tmp_path):
        """Two threads acquiring the same lock are serialized."""
        lock_path = tmp_path / "concurrent.lock"
        results = []
        errors = []

        def worker(worker_id):
            try:
                lf = open_lock_file(lock_path)
                try:
                    acquire_file_lock(lf)
                    results.append(f"start-{worker_id}")
                    results.append(f"end-{worker_id}")
                    release_file_lock(lf)
                finally:
                    lf.close()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []
        # Both workers completed
        assert len(results) == 4


# ---------------------------------------------------------------------------
# open_lock_file
# ---------------------------------------------------------------------------

class TestOpenLockFile:
    """Tests for the open_lock_file function."""

    @pytest.mark.skipif(os.name == "nt", reason="Unix only")
    def test_creates_lock_file(self, tmp_path):
        """open_lock_file creates the file if it doesn't exist."""
        lock_path = tmp_path / "new.lock"
        assert not lock_path.exists()

        lock_file = open_lock_file(lock_path)
        lock_file.close()

        assert lock_path.exists()

    @pytest.mark.skipif(os.name == "nt", reason="Unix only")
    def test_rejects_symlink_lock_path(self, tmp_path):
        """open_lock_file rejects symlinks."""
        target = tmp_path / "real.lock"
        target.write_text("")
        link = tmp_path / "link.lock"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("Symlinks not supported")

        with pytest.raises((OSError, ValueError)):
            open_lock_file(link)
