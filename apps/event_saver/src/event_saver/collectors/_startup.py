"""Shared start-up helpers for the collectors (feature 0039 / 0110 B1c-2)."""

import asyncio
import threading
from typing import Any, Callable, Optional


class CollectorStartError(RuntimeError):
    """A collector could not bring its WebSocket up within its start bound."""


class StartHandoff:
    """Decides who closes the socket when a bounded start gives up.

    The owner (event loop) gives up on a timeout or a cancellation while the
    worker thread may be anywhere in connect / readiness. Both sides go
    through one lock, so exactly one of them closes the socket: the worker
    if the owner gave up first, the owner if the worker had already finished.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._abandoned = False
        self._finished = False

    def abandoned(self) -> bool:
        """Worker: whether the owner has given up (checked mid-way)."""
        with self._lock:
            return self._abandoned

    def finish(self) -> bool:
        """Worker: it is done (returning or raising).

        False → the owner gave up; the worker must close the socket.
        """
        with self._lock:
            if self._abandoned:
                return False
            self._finished = True
            return True

    def abandon(self) -> bool:
        """Owner: give up. True → the worker had finished; close the socket."""
        with self._lock:
            self._abandoned = True
            return self._finished


def disconnect_in_background(client: Any, *, name: str) -> None:
    """Close ``client`` on a daemon thread without waiting for it.

    For the owner side of :class:`StartHandoff`: the start already failed or
    was cancelled, so the (possibly slow) disconnect must not be awaited.
    """
    fut = run_in_daemon_thread(client.disconnect, name=name)
    # Retrieve the outcome so a failed disconnect is not reported as an
    # unretrieved exception.
    fut.add_done_callback(lambda f: f.cancelled() or f.exception())


def run_in_daemon_thread(
    fn: Callable[[], Any], *, name: Optional[str] = None
) -> "asyncio.Future[Any]":
    """Run ``fn`` on a dedicated daemon thread; return a future bound to the loop.

    Used to wrap blocking pybit calls (``connect`` / ``wait_ready`` /
    ``reset`` / ``disconnect``) so they can be bounded by
    ``asyncio.wait_for`` and **abandoned** on timeout without leaking into
    ``concurrent.futures.thread._python_exit`` at interpreter shutdown
    (which would join the worker and re-introduce the hang).

    Cancellation-safety: if ``wait_for`` cancels the future before the thread
    returns, the completer guards on ``fut.done()`` so a late-returning worker
    does not raise ``InvalidStateError`` on the loop. If the loop has been
    closed by the time the worker returns, ``call_soon_threadsafe`` raises
    ``RuntimeError`` which we swallow — the daemon thread exits quietly.

    Args:
        fn: Blocking callable to run on the worker thread.
        name: Optional thread name (diagnostics).

    Returns:
        A future on the running loop with ``fn``'s result or exception.
    """
    loop = asyncio.get_running_loop()
    fut: "asyncio.Future[Any]" = loop.create_future()

    def _complete(result: Any = None, exc: Optional[BaseException] = None) -> None:
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    def _target() -> None:
        try:
            result = fn()
            exc: Optional[BaseException] = None
        except BaseException as e:  # noqa: BLE001 — route to future
            result = None
            exc = e
        try:
            loop.call_soon_threadsafe(_complete, result, exc)
        except RuntimeError:
            # Loop already closed; daemon thread just exits.
            pass

    threading.Thread(target=_target, name=name, daemon=True).start()
    return fut
