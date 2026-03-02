"""Cross-process and in-process locking for the risk-limit cache.

Provides:
  - In-process ``threading.Lock`` management (ref-counted per cache path).
  - Cross-process file locking via ``fcntl.flock`` (Unix) / ``msvcrt.locking``
    (Windows).
"""

import logging
import os
import threading
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

# Lock region size for Windows msvcrt.locking (bytes).
# Must match in both _acquire_file_lock and _release_file_lock.
# 1 KB is sufficient: msvcrt.locking locks a byte range (not the whole
# file), so 1024 bytes provides a wide enough region to prevent
# overlapping partial locks while staying well within any cache file size.
# See README "Concurrent Access" section for performance characteristics.
_LOCK_REGION_BYTES = 1024

# Global registry of per-path in-process locks.
# Key: stringified cache path → (threading.Lock, reference_count).
# The guard serializes registry mutations only; the per-path lock
# serializes actual cache I/O.
_IN_PROCESS_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_IN_PROCESS_LOCKS_GUARD = threading.Lock()


def acquire_in_process_lock(path: Path) -> tuple[str, threading.Lock]:
    """Acquire (or create) a ref-counted in-process lock for *path*."""
    key = str(path)
    with _IN_PROCESS_LOCKS_GUARD:
        entry = _IN_PROCESS_LOCKS.get(key)
        if entry is None:
            lock = threading.Lock()
            _IN_PROCESS_LOCKS[key] = (lock, 1)
        else:
            lock, ref_count = entry
            _IN_PROCESS_LOCKS[key] = (lock, ref_count + 1)
    return key, lock


def release_in_process_lock(key: str) -> None:
    """Decrement the ref-count for *key*; remove when it hits zero."""
    with _IN_PROCESS_LOCKS_GUARD:
        entry = _IN_PROCESS_LOCKS.get(key)
        if entry is None:
            return
        lock, ref_count = entry
        if ref_count <= 1:
            del _IN_PROCESS_LOCKS[key]
        else:
            _IN_PROCESS_LOCKS[key] = (lock, ref_count - 1)


def open_lock_file(lock_path: Path) -> "IO[bytes]":
    """Open lock file safely; reject symlink lock paths.

    Relies on ``O_NOFOLLOW`` to atomically reject symlinks at open time.
    A post-open inode/device check closes the remaining TOCTOU window:
    even though ``O_NOFOLLOW`` prevents *following* a symlink, an attacker
    could swap the real file for a symlink between the ``open()`` return
    and the first ``fstat()``.  The post-open ``lstat`` + ``fstat``
    comparison detects this swap.
    """
    flags = os.O_RDWR | os.O_CREAT
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise NotImplementedError(
            "os.O_NOFOLLOW is not available on this platform; "
            "symlink protection for cache lock files is disabled"
        )
    flags |= nofollow

    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as e:
        # On platforms with O_NOFOLLOW, opening a symlink raises OSError
        try:
            if os.lstat(lock_path).st_mode & 0o170000 == 0o120000:  # S_IFLNK
                raise ValueError("Cache lock path must not be a symlink") from e
        except FileNotFoundError:
            pass
        raise

    # Post-open validation: verify the fd points to the expected path.
    # This is necessary despite O_NOFOLLOW because a symlink swap could
    # occur between os.open() returning and this check.  Comparing the
    # inode/device of the opened fd against lstat() of the path detects
    # such swaps.
    try:
        path_stat = os.lstat(lock_path)
        import stat as stat_mod

        if stat_mod.S_ISLNK(path_stat.st_mode):
            raise ValueError("Cache lock path must not be a symlink")
        fd_stat = os.fstat(fd)
        if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
            raise ValueError("Cache lock path changed during open")
    except (OSError, ValueError):
        os.close(fd)
        raise

    return os.fdopen(fd, "a+b")


def acquire_file_lock(lock_file: IO[bytes]) -> None:
    """Acquire an exclusive lock for cache read-modify-write operations."""
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0, os.SEEK_END)
        file_size = lock_file.tell()
        if file_size < _LOCK_REGION_BYTES:
            lock_file.write(b"\0" * (_LOCK_REGION_BYTES - file_size))
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, _LOCK_REGION_BYTES)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def release_file_lock(lock_file: IO[bytes]) -> None:
    """Release the cache file lock."""
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, _LOCK_REGION_BYTES)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
