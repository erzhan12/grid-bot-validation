"""Tests for the collectors' shared start-up helpers (0110 B1c-2)."""

from event_saver.collectors._startup import StartHandoff


class TestStartHandoff:
    def test_worker_finishes_first_then_owner_must_close(self):
        """The worker claimed success before the owner gave up: the socket
        is live and nobody is waiting for it, so the owner must close it."""
        handoff = StartHandoff()
        assert handoff.finish() is True
        assert handoff.abandon() is True

    def test_owner_gives_up_first_then_worker_must_close(self):
        """The owner gave up first: the worker must close what it opened."""
        handoff = StartHandoff()
        assert handoff.abandon() is False
        assert handoff.abandoned() is True
        assert handoff.finish() is False

    def test_not_abandoned_until_the_owner_gives_up(self):
        """A fresh handoff is not abandoned."""
        assert StartHandoff().abandoned() is False
