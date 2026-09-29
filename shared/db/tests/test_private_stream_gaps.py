"""Tests for PrivateStreamGapRepository (feature 0110 Phase B1a)."""

from datetime import UTC, datetime, timedelta

import pytest

from grid_db import (
    PrivateStreamGap,
    PrivateStreamGapRepository,
    RecoveryStatus,
)


class TestPrivateStreamGapRepository:
    def test_gap_and_outcome_persist(self, session, sample_account, sample_run):
        """A recorded gap and its recovery outcome are read back from the DB."""
        gap_start = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
        gap_end = gap_start + timedelta(seconds=40)
        repo = PrivateStreamGapRepository(session)

        gap = repo.add_gap(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            symbol="LTCUSDT",
            gap_start=gap_start,
            gap_end=gap_end,
        )
        assert gap.recovery_status == RecoveryStatus.PENDING
        repo.set_outcome(
            gap.id,
            run_id=sample_run.run_id,
            status=RecoveryStatus.RECOVERED,
            inserted=3,
            duplicates=1,
            reason=None,
        )
        session.commit()
        session.expire_all()

        row = session.get(PrivateStreamGap, gap.id)
        assert row.run_id == sample_run.run_id
        assert row.account_id == sample_account.account_id
        assert row.symbol == "LTCUSDT"
        assert row.gap_start.replace(tzinfo=UTC) == gap_start
        assert row.gap_end.replace(tzinfo=UTC) == gap_end
        assert row.recovery_status == RecoveryStatus.RECOVERED
        assert (row.inserted, row.duplicates, row.reason) == (3, 1, None)

    def test_failed_outcome_keeps_reason(self, session, sample_account, sample_run):
        """A failed recovery stores its status and a reason string."""
        repo = PrivateStreamGapRepository(session)
        gap = repo.add_gap(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            symbol="LTCUSDT",
            gap_start=datetime(2026, 9, 1, tzinfo=UTC),
            gap_end=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
        )
        repo.set_outcome(
            gap.id,
            run_id=sample_run.run_id,
            status=RecoveryStatus.FAILED,
            inserted=0,
            duplicates=0,
            reason="REST error: boom",
        )
        session.commit()
        session.expire_all()

        row = session.get(PrivateStreamGap, gap.id)
        assert row.recovery_status == RecoveryStatus.FAILED
        assert row.reason == "REST error: boom"

    def test_deleting_run_cascades_to_gaps(
        self, session, sample_account, sample_run
    ):
        """Gap rows go away with their run (recorder startup wipe)."""
        PrivateStreamGapRepository(session).add_gap(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            symbol="LTCUSDT",
            gap_start=datetime(2026, 9, 1, tzinfo=UTC),
            gap_end=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
        )
        session.commit()
        session.delete(sample_run)
        session.commit()
        assert session.query(PrivateStreamGap).count() == 0

    @pytest.mark.parametrize("status", list(RecoveryStatus))
    def test_every_status_round_trips(
        self, session, sample_account, sample_run, status
    ):
        """Each status value survives a commit + reload (fits String(12))."""
        repo = PrivateStreamGapRepository(session)
        gap = repo.add_gap(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            symbol="LTCUSDT",
            gap_start=datetime(2026, 9, 1, tzinfo=UTC),
            gap_end=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
        )
        repo.set_outcome(
            gap.id,
            run_id=sample_run.run_id,
            status=status,
            inserted=0,
            duplicates=0,
            reason=None,
        )
        session.commit()
        session.expire_all()
        assert session.get(PrivateStreamGap, gap.id).recovery_status == status

    def test_set_outcome_is_scoped_by_run(
        self, session, sample_account, sample_run
    ):
        """A gap id under another run is not found and is left untouched."""
        repo = PrivateStreamGapRepository(session)
        gap = repo.add_gap(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            symbol="LTCUSDT",
            gap_start=datetime(2026, 9, 1, tzinfo=UTC),
            gap_end=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="not found"):
            repo.set_outcome(
                gap.id,
                run_id="some-other-run",
                status=RecoveryStatus.FAILED,
                inserted=0,
                duplicates=0,
                reason="x",
            )
        assert gap.recovery_status == RecoveryStatus.PENDING

    def test_set_outcome_on_missing_row_raises(self, session):
        """An unknown gap id is an error, not a silent no-op."""
        with pytest.raises(ValueError, match="not found"):
            PrivateStreamGapRepository(session).set_outcome(
                999999,
                run_id="no-such-run",
                status=RecoveryStatus.FAILED,
                inserted=0,
                duplicates=0,
                reason="x",
            )

    def test_long_reason_is_capped(self, session, sample_account, sample_run):
        """Raw exception text (SQL + params) is bounded to 500 chars."""
        repo = PrivateStreamGapRepository(session)
        gap = repo.add_gap(
            run_id=sample_run.run_id,
            account_id=sample_account.account_id,
            symbol="LTCUSDT",
            gap_start=datetime(2026, 9, 1, tzinfo=UTC),
            gap_end=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
        )
        repo.set_outcome(
            gap.id,
            run_id=sample_run.run_id,
            status=RecoveryStatus.FAILED,
            inserted=0,
            duplicates=0,
            reason="x" * 2000,
        )
        assert len(session.get(PrivateStreamGap, gap.id).reason) == 500

    def test_recovery_status_values(self):
        """The status domain is exactly the five documented values."""
        assert {s.value for s in RecoveryStatus} == {
            "pending", "recovered", "skipped", "truncated", "failed",
        }
