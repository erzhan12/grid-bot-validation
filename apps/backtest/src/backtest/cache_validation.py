"""Cache file validation helpers for the risk-limit cache.

Centralises symlink detection, O_NOFOLLOW opening, size-limit checks,
and inode-identity verification so that ``_load_cache_entry`` and
``_load_existing_cache`` share the same validation logic.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CacheSizeExceededError(ValueError):
    """Raised when the cache file exceeds the configured size limit."""

    pass


def cache_path_is_symlink(configured_path: Path, resolved_path: Path) -> bool:
    """Return True if either the configured or resolved cache path is a symlink.

    Checks both the original configured path (absolute, not resolved) and
    the resolved path to detect symlinks in any parent directory component.
    """
    return configured_path.is_symlink() or resolved_path.is_symlink()


def validate_and_open_cache_file(
    cache_path: Path,
    max_cache_size_bytes: int,
) -> Optional[int]:
    """Open a cache file with symlink protection and size validation.

    Performs the shared validation sequence used by both read paths
    (``_load_cache_entry`` and ``_load_existing_cache``):

      1. Open with ``O_NOFOLLOW`` to atomically reject symlinks.
      2. ``fstat`` the fd to check size against *max_cache_size_bytes*.
      3. Verify the fd's inode/device still matches ``os.lstat(cache_path)``
         to detect symlink swaps in the TOCTOU window.

    Args:
        cache_path: Resolved cache file path.
        max_cache_size_bytes: Upper size limit (0 disables the check).

    Returns:
        An open file descriptor on success, or ``None`` when the file
        does not exist. Raises on validation failure.

    Raises:
        CacheSizeExceededError: If the file exceeds *max_cache_size_bytes*.
        ValueError: If a symlink swap is detected after opening.
        NotImplementedError: If ``O_NOFOLLOW`` is unavailable.
        OSError: If the file is a symlink or inaccessible.
    """
    if not cache_path.exists():
        return None

    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise NotImplementedError(
            "os.O_NOFOLLOW is not available on this platform; "
            "symlink protection for cache files is disabled"
        )
    flags |= nofollow

    fd = os.open(cache_path, flags)  # raises OSError if symlink

    try:
        fd_stat = os.fstat(fd)
        if max_cache_size_bytes and fd_stat.st_size > max_cache_size_bytes:
            raise CacheSizeExceededError(
                f"Cache file size {fd_stat.st_size} exceeds {max_cache_size_bytes} byte limit"
            )
        path_stat = os.lstat(cache_path)
        if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise ValueError(
                "Cache file identity changed during open (possible symlink swap)"
            )
    except BaseException:
        os.close(fd)
        raise

    return fd


def read_cache_from_fd(fd: int) -> Any:
    """Read and parse JSON from an already-validated file descriptor.

    Consumes the fd — it will be closed when the function returns
    (on success or failure).

    Returns:
        The parsed JSON object.

    Raises:
        json.JSONDecodeError: On malformed JSON.
        ValueError: If the root element is not a dict.
    """
    try:
        with os.fdopen(fd, "r") as f:
            cache = json.load(f)
        # fd is now owned and closed by the file object
        if not isinstance(cache, dict):
            raise ValueError("Cache root must be a JSON object")
        return cache
    except BaseException:
        # fd may already be closed by fdopen; ignore double-close errors
        try:
            os.close(fd)
        except OSError:
            pass
        raise
