"""
Grid state persistence for maintaining full grid levels across restarts.

This module provides file-based persistence for grid state, keyed by strat_id
to support multiple strategy instances.

Save semantics:
- Sync API, non-blocking: cheap-fingerprint dedupe + a daemon
  threading.Thread for the actual disk I/O. Atomic writes via tmp +
  os.replace + fsync.
- Single-writer-per-strat: a per-strat pending slot holds the latest payload;
  if a writer is already running for that strat, save() just updates the slot
  and the in-flight writer drains it. This guarantees latest-wins ordering
  per strat (threading.Lock by itself is not FIFO, so a naive "spawn-a-thread-
  per-save" design can write payloads out of order).

Threads (rather than asyncio) are used because the live gridbot orchestrator
runs a synchronous main loop (time.sleep), so asyncio.create_task would
always fall through to the sync path and block the loop on fsync.
"""

import copy
import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class GridStateStore:
    """
    File-based storage for full grid state.

    Stores the full ordered grid (list of {side, price}) per strat_id along with
    grid_step and grid_count for config-mismatch invalidation.

    Reference: bbu2-master/db_files.py greed.json schema (array form);
    we use a dict-per-strat_id shape (carried over from the legacy format).
    """

    def __init__(self, file_path: str = 'db/grid_anchor.json'):
        """
        Initialize state store.

        Args:
            file_path: Path to JSON file for storing grid state. The default
                       name is preserved from the legacy GridAnchorStore to
                       avoid disturbing deploy configs.
        """
        self.file_path = file_path
        # Cheap fingerprint of the last-enqueued payload per strat_id, used to
        # short-circuit identical save() calls without paying for deepcopy.
        self._last_fingerprint: dict[str, tuple] = {}
        # The latest payload waiting to be written, per strat_id. A new save()
        # overwrites the slot — the in-flight writer (if any) will pick up the
        # newer value the next time it loops, so a burst of saves coalesces
        # into one final disk write per strat.
        self._pending_payload: dict[str, dict] = {}
        # State + condition variable for: serializing slot access, dedupe
        # bookkeeping, the active-writer set, and flush() wait/notify.
        self._cv = threading.Condition()
        # strat_ids that currently have a writer thread running.
        self._active_writers: set[str] = set()
        # Serializes concurrent disk I/O across strats (the file is shared).
        self._io_lock = threading.Lock()

    def _validate_strat_id(self, strat_id: str) -> None:
        if strat_id == "":
            raise ValueError("strat_id must be a non-empty string")

    @staticmethod
    def _fingerprint(grid: list[dict], grid_step: float, grid_count: int) -> tuple:
        """Cheap structural identity of a payload — comparable, hashable,
        and ~50x cheaper than deepcopy + dict equality."""
        return (
            tuple((g['side'], g['price']) for g in grid),
            grid_step,
            grid_count,
        )

    def _read_all_data(self) -> dict:
        """Read the full JSON file, returning {} on any error or if the root
        is not a JSON object. A hand-edited file with a list/string/number
        root would otherwise crash load()/save()/delete() with AttributeError
        or TypeError; this helper makes the persistence layer self-healing."""
        if not os.path.exists(self.file_path):
            return {}
        try:
            with open(self.file_path, 'r') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def load(self, strat_id: str) -> Optional[dict]:
        """
        Load grid state for a strategy.

        Returns the full saved entry (with `grid`, `grid_step`, `grid_count`)
        or None if not found, corrupted, or in the legacy anchor-only format.

        Legacy format detection: if the entry has no `grid` key it is treated
        as no saved state and a one-time info log is emitted; the engine will
        rebuild from market price on the first ticker.

        Args:
            strat_id: Strategy identifier

        Returns:
            Saved entry dict (with `grid`, `grid_step`, `grid_count`) or None.
        """
        self._validate_strat_id(strat_id)
        all_data = self._read_all_data()

        entry = all_data.get(strat_id)
        if entry is None:
            return None

        # Defensive: a hand-edited or otherwise malformed file may have an
        # entry that isn't a dict (e.g. {"strat_id": 1}). Treat it the same
        # as corruption — fall back to a fresh grid instead of crashing.
        if not isinstance(entry, dict):
            return None

        if 'grid' not in entry:
            logger.info(
                "%s: Legacy anchor format ignored, building fresh grid at market price",
                strat_id,
            )
            return None

        return entry

    def save(self, strat_id: str, grid: list[dict], grid_step: float, grid_count: int) -> None:
        """
        Save grid state for a strategy.

        Sync wrapper: cheap-fingerprint dedupe and then dispatch via a per-strat
        pending slot. Returns immediately; does not block on fsync.

        If a writer thread is already running for this strat_id, this call just
        updates the slot — the in-flight writer drains it. Otherwise a fresh
        writer thread is spawned. Net effect: latest-wins per strat, no write
        reordering across rapid bursts.

        Args:
            strat_id: Strategy identifier
            grid: Full grid as list of {side, price} dicts
            grid_step: Grid step size (for config-mismatch invalidation)
            grid_count: Number of grid levels (for config-mismatch invalidation)
        """
        self._validate_strat_id(strat_id)

        fingerprint = self._fingerprint(grid, grid_step, grid_count)
        # Deep-copy lazily, only after the dedupe check decides we'll persist.
        spawn = False
        with self._cv:
            if self._last_fingerprint.get(strat_id) == fingerprint:
                return
            self._last_fingerprint[strat_id] = fingerprint

            payload = {
                'grid': copy.deepcopy(grid),
                'grid_step': grid_step,
                'grid_count': grid_count,
            }
            # Pair the fingerprint with the payload so the writer can roll
            # back the dedupe key on failure without clobbering a newer
            # enqueued payload that arrived in the meantime.
            self._pending_payload[strat_id] = (fingerprint, payload)

            if strat_id not in self._active_writers:
                self._active_writers.add(strat_id)
                spawn = True

        if spawn:
            threading.Thread(
                target=self._writer_loop,
                args=(strat_id,),
                daemon=True,
            ).start()

    def _writer_loop(self, strat_id: str) -> None:
        """Drain the per-strat pending slot until empty, then exit. Each
        iteration writes the latest payload; concurrent saves arriving while
        the previous write is in flight just overwrite the slot and the next
        loop iteration picks them up. Errors are logged, never propagated —
        persistence failures must not crash strategy logic."""
        try:
            while True:
                with self._cv:
                    item = self._pending_payload.pop(strat_id, None)
                    if item is None:
                        self._active_writers.discard(strat_id)
                        self._cv.notify_all()
                        return
                fingerprint, payload = item
                try:
                    with self._io_lock:
                        self._sync_write_to_disk(strat_id, payload)
                except Exception as e:
                    logger.error("Save failed for %s: %s", strat_id, e)
                    # Roll back the dedupe key so the next identical save()
                    # is not silently skipped — but only if no newer payload
                    # has been enqueued in the meantime (whose fingerprint
                    # would have replaced ours in _last_fingerprint).
                    with self._cv:
                        if self._last_fingerprint.get(strat_id) == fingerprint:
                            self._last_fingerprint.pop(strat_id, None)
        except BaseException:
            # Defensive: even on unexpected failure, release the active-writer
            # slot so future saves can spawn a new thread.
            with self._cv:
                self._active_writers.discard(strat_id)
                self._cv.notify_all()
            raise

    def flush(self, timeout: Optional[float] = None) -> None:
        """Block until all in-flight background writes have completed.

        Useful in tests and for graceful shutdown. Production callers do not
        need to invoke this — daemon threads either complete or die with the
        process, and the next save() will retry (the write itself is atomic).
        """
        with self._cv:
            self._cv.wait_for(lambda: not self._active_writers, timeout=timeout)

    def _sync_write_to_disk(self, strat_id: str, payload: dict) -> None:
        """Atomic write: tmp file + fsync + os.replace. Either the new file
        fully replaces the old one or nothing happens — a kill -9 mid-write
        cannot leave a corrupted half-written file in place. A corrupt or
        non-dict-root file is silently overwritten with a fresh dict."""
        all_data = self._read_all_data()
        all_data[strat_id] = payload

        dir_path = os.path.dirname(self.file_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path)

        tmp_path = self.file_path + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(all_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.file_path)

    def delete(self, strat_id: str) -> bool:
        """
        Delete grid state for a strategy.

        Atomic (uses the same tmp + os.replace pattern as save) and serialized
        through the same I/O lock that gates background writes, so it cannot
        race with an in-flight write to the shared file.

        Returns True if deleted, False if not found.
        """
        self._validate_strat_id(strat_id)
        if not os.path.exists(self.file_path):
            return False

        with self._io_lock:
            all_data = self._read_all_data()
            if strat_id not in all_data:
                return False

            del all_data[strat_id]

            tmp_path = self.file_path + '.tmp'
            try:
                with open(tmp_path, 'w') as f:
                    json.dump(all_data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.file_path)
            except (IOError, OSError):
                return False

        with self._cv:
            self._last_fingerprint.pop(strat_id, None)
            self._pending_payload.pop(strat_id, None)

        return True
