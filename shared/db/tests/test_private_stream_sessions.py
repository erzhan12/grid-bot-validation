"""Tests for private-stream coverage sessions and open gaps (0110 B1c-1)."""

from datetime import UTC, datetime, timedelta

import pytest

from grid_db import (
    PrivateStreamGap,
    PrivateStreamGapRepository,
    PrivateStreamSession,
    PrivateStreamSessionRepository,
)

_T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


class TestPrivateStreamSessionRepository:
    def test_session_and_checkpoint_round_trip(
        self, session, sample_account, sample_run
    ):
        """A session row and its advanced checkpoint read back from the DB."""
        repo = PrivateStreamSessionRepository(session)
        row = repo.open_session(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            connected_at=_T0,
        )
        assert row.last_checkpoint_ts == _T0
        repo.advance_checkpoint(
            row.id, run_id=sample_run.run_id, ts=_T0 + timedelta(minutes=5)
        )
        session.commit()
        session.expire_all()

        got = session.get(PrivateStreamSession, row.id)
        assert got.run_id == sample_run.run_id
        assert got.account_id == sample_account.account_id
        assert got.connected_at.replace(tzinfo=UTC) == _T0
        assert got.last_checkpoint_ts.replace(tzinfo=UTC) == _T0 + timedelta(
            minutes=5
        )

    def test_checkpoint_never_moves_backward(
        self, session, sample_account, sample_run
    ):
        """An older checkpoint is ignored; the later one stays."""
        repo = PrivateStreamSessionRepository(session)
        row = repo.open_session(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            connected_at=_T0,
        )
        later = _T0 + timedelta(minutes=5)
        repo.advance_checkpoint(row.id, run_id=sample_run.run_id, ts=later)
        repo.advance_checkpoint(
            row.id, run_id=sample_run.run_id, ts=_T0 + timedelta(minutes=1)
        )
        session.commit()
        session.expire_all()
        got = session.get(PrivateStreamSession, row.id)
        assert got.last_checkpoint_ts.replace(tzinfo=UTC) == later

    def test_advance_checkpoint_is_scoped_by_run(
        self, session, sample_account, sample_run
    ):
        """A session id under another run is not found."""
        repo = PrivateStreamSessionRepository(session)
        row = repo.open_session(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            connected_at=_T0,
        )
        with pytest.raises(ValueError, match="not found"):
            repo.advance_checkpoint(row.id, run_id="other-run", ts=_T0)

    def test_deleting_run_cascades_to_sessions(
        self, session, sample_account, sample_run
    ):
        """Session rows go away with their run (recorder startup wipe)."""
        PrivateStreamSessionRepository(session).open_session(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            connected_at=_T0,
        )
        session.commit()
        session.delete(sample_run)
        session.commit()
        assert session.query(PrivateStreamSession).count() == 0


class TestOpenGaps:
    def test_open_gap_then_close(self, session, sample_account, sample_run):
        """A gap opened with no end is closed later with its end time."""
        repo = PrivateStreamGapRepository(session)
        gap = repo.add_gap(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            symbol="LTCUSDT",
            gap_start=_T0,
            gap_end=None,
        )
        session.commit()
        session.expire_all()
        assert session.get(PrivateStreamGap, gap.id).gap_end is None

        repo.close_gap(
            gap.id, run_id=sample_run.run_id, gap_end=_T0 + timedelta(seconds=90)
        )
        session.commit()
        session.expire_all()
        got = session.get(PrivateStreamGap, gap.id)
        assert got.gap_end.replace(tzinfo=UTC) == _T0 + timedelta(seconds=90)

    def test_close_gap_is_scoped_by_run(self, session, sample_account, sample_run):
        """A gap id under another run is not found and stays open."""
        repo = PrivateStreamGapRepository(session)
        gap = repo.add_gap(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            symbol="LTCUSDT",
            gap_start=_T0,
            gap_end=None,
        )
        with pytest.raises(ValueError, match="not found"):
            repo.close_gap(gap.id, run_id="other-run", gap_end=_T0)
        assert gap.gap_end is None
