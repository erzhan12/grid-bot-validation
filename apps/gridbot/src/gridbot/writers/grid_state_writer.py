"""DB writer for verbatim grid-state snapshots (feature 0047).

Mirrors ``gridcore.persistence.GridStateStore``'s sync-API + background-thread
pattern. **Not** asyncio — gridbot's main loop is synchronous.

Concurrency model:
* One global FIFO ``queue.Queue`` per writer instance, drained by a single
  worker thread; INSERTs land in dequeue order so the loader's
  ``ORDER BY exchange_ts DESC, id DESC`` tie-break picks the FINAL notify
  of a multi-notify outer mutation (e.g. ``update_grid`` out-of-bounds path).
* In-memory tuple dedupe is a separate gate run BEFORE enqueue: if the new
  ``grid_fingerprint(...)`` tuple equals the last-enqueued tuple for the
  same ``(run_id, account_id, strat_id)``, the write is dropped pre-queue.
  Latest-wins-per-strat semantic — same as the file writer.
* If ``run_id_provider(strat_id)`` returns ``None`` (orchestrator hasn't
  populated ``_run_ids`` yet), or ``exchange_ts`` is ``None`` (no triggering
  event in scope), the snapshot is dropped with an INFO log.
* ``get_last_fingerprint`` / ``prime_fingerprint`` support startup bootstrap
  (issue #108): probe persisted state and prime dedupe without a redundant row.
"""

import logging
import queue
import threading
from datetime import datetime, UTC
from decimal import Decimal
from typing import Callable, Optional

from grid_db import DatabaseFactory
from grid_db.models import GridStateSnapshot
from grid_db.repositories import GridStateSnapshotRepository
from gridcore.persistence import grid_fingerprint, grid_fingerprint_hash


logger = logging.getLogger(__name__)


# Sentinel pushed onto the queue by ``stop()`` to wake the worker for a
# clean exit.
_STOP_SENTINEL = object()


class GridStateWriter:
    """Persists ``grid.grid`` mutations to ``grid_state_snapshots``.

    Lifecycle:
        writer = GridStateWriter(db, run_id_provider=lambda sid: run_ids.get(sid))
        writer.start()              # launches worker thread

        # In _on_grid_change:
        writer.write(strat_id, grid, grid_step, grid_count,
                     account_id, symbol, exchange_ts)

        writer.flush(timeout=10.0)   # drain queue
        writer.stop()                # signal worker, join thread
    """

    def __init__(
        self,
        db: DatabaseFactory,
        run_id_provider: Callable[[str], Optional[str]],
        max_queue_size: int = 0,
    ):
        """Initialize writer.

        Args:
            db: DatabaseFactory for session management.
            run_id_provider: Callable that maps ``strat_id`` to a ``run_id``
                or ``None`` if the orchestrator hasn't populated it yet
                (bootstrap window before ``_create_run_records``). Late-bound
                resolution decouples writer construction from run-record
                creation; see plan 0047 v18 Phase 2B.
            max_queue_size: Upper bound on pending snapshots. 0 = unbounded
                (default — grid mutations are rare relative to ticker rate).
        """
        self._db = db
        self._run_id_provider = run_id_provider
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        # Per-scope last-enqueued fingerprint tuple; guards pre-enqueue
        # dedupe so identical successive mutations don't bloat the queue.
        self._last_fingerprint: dict[tuple[str, str, str], tuple] = {}
        # Lock guards _last_fingerprint only — the queue itself is
        # thread-safe.
        self._dedupe_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        # One-time log gate for the "run_id not yet set" bootstrap window.
        self._warned_missing_run_id: set[str] = set()

        # Stats
        self._total_written = 0
        self._total_dropped_no_run_id = 0
        self._total_dropped_no_ts = 0
        self._total_dedup_skipped = 0
        self._total_errors = 0
        self._total_bootstrap_failures = 0

    def write(
        self,
        strat_id: str,
        grid: list[dict],
        grid_step: float,
        grid_count: int,
        account_id: str,
        symbol: str,
        exchange_ts: Optional[datetime],
    ) -> None:
        """Enqueue a snapshot (drops on missing run_id, missing ts, or dedupe).

        Note: no ``run_id`` parameter at the call site — the writer resolves
        it via ``run_id_provider`` at enqueue time so the runner does not
        need to know about run lifecycle.
        """
        if exchange_ts is None:
            self._total_dropped_no_ts += 1
            return

        run_id = self._run_id_provider(strat_id)
        if run_id is None:
            self._total_dropped_no_run_id += 1
            if strat_id not in self._warned_missing_run_id:
                self._warned_missing_run_id.add(strat_id)
                logger.info(
                    "%s: skipped DB snapshot — run_id not yet set", strat_id,
                )
            return

        scope = (run_id, account_id, strat_id)
        fp_tuple = grid_fingerprint(grid, grid_step, grid_count)
        with self._dedupe_lock:
            if self._last_fingerprint.get(scope) == fp_tuple:
                self._total_dedup_skipped += 1
                return
            self._last_fingerprint[scope] = fp_tuple

        fp_hash = grid_fingerprint_hash(grid, grid_step, grid_count)
        local_ts = datetime.now(UTC)
        snapshot = GridStateSnapshot(
            run_id=run_id,
            account_id=account_id,
            strat_id=strat_id,
            symbol=symbol,
            exchange_ts=exchange_ts,
            local_ts=local_ts,
            # Defensive copy: callers may mutate ``grid`` in place between
            # enqueue and worker drain.
            grid_json=[dict(level) for level in grid],
            grid_step=Decimal(str(grid_step)),
            grid_count=grid_count,
            raw_fingerprint=fp_hash,
        )
        # Pair scope + fingerprint with the snapshot so the worker can roll
        # back the dedupe key on insert failure without clobbering a newer
        # enqueued payload that arrived in the meantime (mirrors
        # GridStateStore._writer_loop semantics).
        self._queue.put((snapshot, scope, fp_tuple))

    def start(self) -> None:
        """Launch the worker thread."""
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="GridStateWriter",
            daemon=True,
        )
        self._worker.start()
        logger.info("GridStateWriter worker started")

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until all queued snapshots have been processed.

        Uses ``queue.join`` semantics (worker calls ``task_done`` after each
        insert). On timeout the remaining snapshots stay queued — caller may
        retry by calling ``flush`` again.

        Returns:
            ``True`` when ``queue.join()`` completed within ``timeout``,
            ``False`` on timeout.
        """
        deadline_event = threading.Event()

        def _wait() -> None:
            self._queue.join()
            deadline_event.set()

        waiter = threading.Thread(target=_wait, daemon=True)
        waiter.start()
        if not deadline_event.wait(timeout=timeout):
            logger.warning(
                "GridStateWriter.flush timed out after %.1fs with ~%d items pending",
                timeout, self._queue.qsize(),
            )
            return False
        return True

    def get_last_fingerprint(
        self,
        run_id: str,
        account_id: str,
        strat_id: str,
    ) -> Optional[tuple[tuple, datetime]]:
        """Return ``(grid_fingerprint(...), exchange_ts)`` of the latest row, or ``None``."""
        with self._db.get_session() as session:
            row = GridStateSnapshotRepository(session).get_latest(
                run_id, account_id, strat_id,
            )
            if row is None:
                return None
            return (
                grid_fingerprint(row.grid_json, float(row.grid_step), row.grid_count),
                row.exchange_ts,
            )

    def prime_fingerprint(self, scope: tuple[str, str, str], fp_tuple: tuple) -> None:
        """Seed the in-memory dedupe gate without enqueueing a snapshot."""
        with self._dedupe_lock:
            self._last_fingerprint[scope] = fp_tuple

    def increment_bootstrap_failures(self) -> None:
        """Bump the bootstrap-failure counter (called by orchestrator bootstrap)."""
        self._total_bootstrap_failures += 1

    def stop(self) -> None:
        """Signal worker to drain and exit, then join."""
        if not self._running:
            return
        self._running = False
        self._queue.put(_STOP_SENTINEL)
        if self._worker is not None:
            self._worker.join(timeout=10.0)
        logger.info(
            "GridStateWriter stopped. written=%d dedup_skipped=%d "
            "dropped_no_run_id=%d dropped_no_ts=%d errors=%d",
            self._total_written, self._total_dedup_skipped,
            self._total_dropped_no_run_id, self._total_dropped_no_ts,
            self._total_errors,
        )

    def _worker_loop(self) -> None:
        """Drain the queue, one snapshot per session-and-commit.

        Per-snapshot commits trade throughput for safety: a poison snapshot
        cannot block subsequent ones from landing, and ``orchestrator.stop()``
        flush is bounded by the number of inflight items.
        """
        while True:
            item = self._queue.get()
            try:
                if item is _STOP_SENTINEL:
                    return
                snapshot, scope, fp_tuple = item
                self._insert_one(snapshot, scope, fp_tuple)
            finally:
                self._queue.task_done()

    def _insert_one(
        self,
        snapshot: GridStateSnapshot,
        scope: tuple[str, str, str],
        fp_tuple: tuple,
    ) -> None:
        try:
            with self._db.get_session() as session:
                repo = GridStateSnapshotRepository(session)
                inserted = repo.insert(snapshot)
                if inserted:
                    self._total_written += 1
                else:
                    # Partial-index conflict — silent no-op is correct
                    # (race-double-insert protection).
                    self._total_dedup_skipped += 1
        except Exception as e:
            self._total_errors += 1
            logger.error("GridStateWriter insert failed: %s", e, exc_info=True)
            # Roll back the in-memory dedupe key so a retry with the same
            # state can still reach the DB. Race-safe: only pop if a newer
            # enqueue has not already replaced the value — otherwise we'd
            # clobber the in-flight newer payload's gate.
            with self._dedupe_lock:
                if self._last_fingerprint.get(scope) == fp_tuple:
                    self._last_fingerprint.pop(scope, None)

    def get_stats(self) -> dict:
        return {
            "total_written": self._total_written,
            "total_dedup_skipped": self._total_dedup_skipped,
            "total_dropped_no_run_id": self._total_dropped_no_run_id,
            "total_dropped_no_ts": self._total_dropped_no_ts,
            "total_errors": self._total_errors,
            "total_bootstrap_failures": self._total_bootstrap_failures,
            "queue_size": self._queue.qsize(),
        }
