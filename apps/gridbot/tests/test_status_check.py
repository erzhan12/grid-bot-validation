"""Unit tests for gridbot.status_check (feature 0109 / issue #258 Phase 1).

Hermetic: all filesystem access goes through pytest's ``tmp_path``, ``now`` is
always injected, and there is no network/DB access.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, UTC

import pytest

from gridbot import status_check
from gridbot.config import GridbotConfig
from gridbot.health import HealthState, HealthMetrics, HealthStatusWriter, build_snapshot
from gridbot.status_check import (
    StatusReason,
    StatusVerdict,
    check_status,
    main,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _write(tmp_path, name, obj_or_text):
    """Write ``obj_or_text`` (dict -> JSON, str -> raw text) under tmp_path."""
    path = tmp_path / name
    if isinstance(obj_or_text, (bytes,)):
        path.write_bytes(obj_or_text)
    elif isinstance(obj_or_text, str):
        path.write_text(obj_or_text)
    else:
        path.write_text(json.dumps(obj_or_text))
    return str(path)


def _snap(state="healthy", generated_at=None, **extra):
    """Build a minimal valid status snapshot dict."""
    if generated_at is None:
        generated_at = NOW.isoformat()
    doc = {"state": state, "generated_at": generated_at}
    doc.update(extra)
    return doc


class TestCheckStatusRejects:
    """Slice 1 — missing / malformed."""

    def test_missing_file(self, tmp_path):
        """A nonexistent path is MISSING, not healthy."""
        path = str(tmp_path / "nope.json")
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MISSING
        assert not verdict.healthy

    def test_empty_file(self, tmp_path):
        """An empty file is MALFORMED (not valid JSON)."""
        path = _write(tmp_path, "status.json", "")
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_invalid_json(self, tmp_path):
        """Non-JSON content is MALFORMED."""
        path = _write(tmp_path, "status.json", "{not json")
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_top_level_array(self, tmp_path):
        """A top-level JSON array (not object) is MALFORMED."""
        path = _write(tmp_path, "status.json", "[1, 2, 3]")
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_unreadable_path_is_directory(self, tmp_path):
        """Passing a directory (OSError on open, not FileNotFoundError) is MALFORMED."""
        verdict = check_status(str(tmp_path), now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED
        assert "unreadable" in verdict.detail

    def test_generated_at_missing(self, tmp_path):
        """A snapshot with no generated_at key is MALFORMED."""
        doc = {"state": "healthy"}
        path = _write(tmp_path, "status.json", doc)
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_generated_at_not_string(self, tmp_path):
        """A non-string generated_at is MALFORMED."""
        doc = {"state": "healthy", "generated_at": 12345}
        path = _write(tmp_path, "status.json", doc)
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_generated_at_unparseable(self, tmp_path):
        """A generated_at string datetime.fromisoformat cannot parse is MALFORMED."""
        path = _write(tmp_path, "status.json", _snap(generated_at="not-a-date"))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_generated_at_naive(self, tmp_path):
        """A generated_at with no tzinfo is MALFORMED."""
        naive = datetime(2026, 9, 24, 12, 0).isoformat()
        path = _write(tmp_path, "status.json", _snap(generated_at=naive))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_invalid_utf8_bytes(self, tmp_path):
        """A raw invalid-UTF-8 byte inside an otherwise-valid JSON body is MALFORMED, not an exception."""
        body = b'{"state": "healthy", "generated_at": "2026-09-24T12:00:00+00:00", "x": "\xff"}'
        path = tmp_path / "status.json"
        path.write_bytes(body)
        verdict = check_status(str(path), now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_deeply_nested_json(self, tmp_path):
        """Pathologically nested JSON (RecursionError) is MALFORMED, not an uncaught exception."""
        path = _write(tmp_path, "status.json", "[" * 100_000)
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_state_missing(self, tmp_path):
        """A snapshot with no state key is MALFORMED."""
        doc = {"generated_at": NOW.isoformat()}
        path = _write(tmp_path, "status.json", doc)
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_state_unknown_value(self, tmp_path):
        """A state string that isn't a HealthState value is MALFORMED."""
        path = _write(tmp_path, "status.json", _snap(state="bogus"))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    @pytest.mark.parametrize("bad_state", [None, 1, ["healthy"]])
    def test_state_not_string(self, tmp_path, bad_state):
        """A non-string state (null / int / list) is MALFORMED, not an exception."""
        doc = {"state": bad_state, "generated_at": NOW.isoformat()}
        path = _write(tmp_path, "status.json", doc)
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED


class TestCheckStatusFreshness:
    """Slice 2 — freshness (core acceptance: stale-file rejection)."""

    def test_stale_healthy_rejected(self, tmp_path):
        """A healthy snapshot older than the threshold is STALE."""
        generated_at = (NOW - timedelta(seconds=61)).isoformat()
        path = _write(tmp_path, "status.json", _snap("healthy", generated_at))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.STALE
        assert verdict.age_seconds == pytest.approx(61, abs=0.01)

    def test_stale_wins_over_unhealthy(self, tmp_path):
        """A stale + already-unhealthy snapshot reports STALE (freshness gates first)."""
        generated_at = (NOW - timedelta(seconds=300)).isoformat()
        path = _write(tmp_path, "status.json", _snap("circuit_open", generated_at))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.STALE

    def test_malformed_wins_over_stale(self, tmp_path):
        """An old snapshot with an unrecognized state is MALFORMED, not STALE.

        Pins "first failure wins": the document is validated before freshness.
        """
        generated_at = (NOW - timedelta(seconds=300)).isoformat()
        path = _write(tmp_path, "status.json", _snap("bogus", generated_at))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_age_equal_threshold_is_fresh(self, tmp_path):
        """Age exactly equal to max_age_seconds is fresh (strict > for staleness)."""
        generated_at = (NOW - timedelta(seconds=60)).isoformat()
        path = _write(tmp_path, "status.json", _snap("healthy", generated_at))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.OK

    def test_custom_threshold(self, tmp_path):
        """A custom (smaller) max_age_seconds makes a 20s-old snapshot stale."""
        generated_at = (NOW - timedelta(seconds=20)).isoformat()
        path = _write(tmp_path, "status.json", _snap("healthy", generated_at))
        verdict = check_status(path, now=NOW, max_age_seconds=10)
        assert verdict.reason == StatusReason.STALE

    def test_future_timestamp_beyond_threshold_rejected(self, tmp_path):
        """A generated_at more than the threshold in the future is MALFORMED."""
        generated_at = (NOW + timedelta(seconds=120)).isoformat()
        path = _write(tmp_path, "status.json", _snap("healthy", generated_at))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.MALFORMED

    def test_small_future_skew_ok(self, tmp_path):
        """A small future clock skew (well within threshold) is OK."""
        generated_at = (NOW + timedelta(seconds=2)).isoformat()
        path = _write(tmp_path, "status.json", _snap("healthy", generated_at))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.OK

    def test_future_exactly_threshold_ok(self, tmp_path):
        """A future generated_at exactly at the threshold is OK (strict > on the future side too)."""
        generated_at = (NOW + timedelta(seconds=60)).isoformat()
        path = _write(tmp_path, "status.json", _snap("healthy", generated_at))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.OK

    @pytest.mark.parametrize(
        "now, max_age_seconds",
        [
            (datetime(2026, 9, 24, 12, 0), 60),  # naive now
            (NOW, 0),
            (NOW, -1),
            (NOW, float("nan")),
            (NOW, float("inf")),
        ],
    )
    def test_invalid_args_raise(self, tmp_path, now, max_age_seconds):
        """Invalid now/max_age_seconds raise ValueError, independent of file content."""
        path = _write(tmp_path, "status.json", _snap())
        with pytest.raises(ValueError):
            check_status(path, now=now, max_age_seconds=max_age_seconds)


class TestCheckStatusState:
    """Slice 3 — state classification."""

    def test_fresh_healthy_ok(self, tmp_path):
        """A fresh healthy snapshot is OK."""
        path = _write(tmp_path, "status.json", _snap("healthy"))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.OK

    def test_fresh_starting_ok(self, tmp_path):
        """A fresh starting snapshot is OK (written once, superseded ~10s later)."""
        path = _write(tmp_path, "status.json", _snap("starting"))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.OK

    @pytest.mark.parametrize("state", ["degraded", "auth_cooldown", "circuit_open"])
    def test_fresh_non_healthy_rejected(self, tmp_path, state):
        """A fresh but non-healthy recognized state is UNHEALTHY, state echoed."""
        path = _write(tmp_path, "status.json", _snap(state))
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.UNHEALTHY
        assert verdict.state == state

    def test_extra_keys_ignored(self, tmp_path):
        """Unknown top-level keys (e.g. a future reasons[] field) don't affect the verdict."""
        path = _write(
            tmp_path, "status.json", _snap("healthy", reasons=["x"], metrics={"a": 1})
        )
        verdict = check_status(path, now=NOW, max_age_seconds=60)
        assert verdict.reason == StatusReason.OK


class TestProducerRoundTrip:
    """Slice 4 — producer/consumer round-trip (contract compatibility).

    Contract/characterization slice: expected to pass on first run against the
    unmodified slice 1-3 code; no production change lands in this slice. Its
    "red" was made observable once via two temporary, manually-run mutation
    probes (drop STARTING from _OK_STATES; loosen the freshness comparison),
    each confirmed failing and then restored — they are not committed tests.
    """

    def _write_real_snapshot(self, tmp_path, *, overall=None, generated_at=None):
        path = str(tmp_path / "status.json")
        writer = HealthStatusWriter(path)
        snapshot = build_snapshot(
            strat_states=[
                {"strat_id": "ltcusdt_test", "state": HealthState.HEALTHY, "shadow": False}
            ],
            metrics=HealthMetrics(),
            gauges={"runners": 1},
            generated_at=(generated_at or NOW).isoformat(),
            overall=overall,
        )
        writer.write(snapshot)
        return path

    def test_fresh_healthy_round_trip_ok(self, tmp_path):
        """A real build_snapshot + HealthStatusWriter healthy snapshot is OK when fresh."""
        path = self._write_real_snapshot(tmp_path)
        verdict = check_status(path, now=NOW + timedelta(seconds=5), max_age_seconds=60)
        assert verdict.reason == StatusReason.OK

    def test_stale_round_trip_stale(self, tmp_path):
        """A real snapshot older than the threshold is STALE."""
        path = self._write_real_snapshot(tmp_path)
        verdict = check_status(path, now=NOW + timedelta(seconds=61), max_age_seconds=60)
        assert verdict.reason == StatusReason.STALE

    def test_starting_round_trip_ok(self, tmp_path):
        """A real snapshot forced to overall=STARTING is OK while fresh."""
        path = self._write_real_snapshot(tmp_path, overall=HealthState.STARTING)
        verdict = check_status(path, now=NOW + timedelta(seconds=5), max_age_seconds=60)
        assert verdict.reason == StatusReason.OK


class TestMain:
    """Slice 5 — CLI."""

    def test_main_ok_exit_zero(self, tmp_path, capsys):
        """A fresh healthy file: exit 0, stdout is exactly one 'OK state=healthy' line."""
        path = _write(tmp_path, "status.json", _snap("healthy", datetime.now(UTC).isoformat()))
        rc = main(["--path", path])
        out, err = capsys.readouterr()
        assert rc == 0
        assert out.startswith("OK state=healthy")
        assert out.count("\n") == 1
        assert err == ""

    def test_main_missing_exit_one(self, tmp_path, capsys):
        """A missing file: exit 1, stdout is one 'ALERT reason=missing' line naming the path."""
        path = str(tmp_path / "nope.json")
        rc = main(["--path", path])
        out, err = capsys.readouterr()
        assert rc == 1
        assert out.startswith("ALERT reason=missing")
        assert "state=- age=-s" in out
        assert path in out
        assert out.count("\n") == 1
        assert err == ""

    def test_main_stale_exit_one(self, tmp_path, capsys):
        """A stale file (generated_at 1h old): exit 1, stdout is one 'ALERT reason=stale' line."""
        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        path = _write(tmp_path, "status.json", _snap("healthy", old))
        rc = main(["--path", path])
        out, err = capsys.readouterr()
        assert rc == 1
        assert "reason=stale" in out
        assert out.count("\n") == 1
        assert err == ""

    def test_main_unhealthy_exit_one(self, tmp_path, capsys):
        """A fresh but unhealthy state: exit 1, single-line ALERT, no stderr."""
        path = _write(tmp_path, "status.json", _snap("circuit_open", datetime.now(UTC).isoformat()))
        rc = main(["--path", path])
        out, err = capsys.readouterr()
        assert rc == 1
        assert "reason=unhealthy" in out
        assert out.count("\n") == 1
        assert err == ""

    def test_main_malformed_exit_one(self, tmp_path, capsys):
        """A malformed file: exit 1, single-line ALERT, no stderr."""
        path = _write(tmp_path, "status.json", "not json")
        rc = main(["--path", path])
        out, err = capsys.readouterr()
        assert rc == 1
        assert "reason=malformed" in out
        assert out.count("\n") == 1
        assert err == ""

    def test_module_entrypoint_subprocess(self, tmp_path):
        """`python -m gridbot.status_check` runs the __main__ guard as a real subprocess.

        Proves the guard exists: without it, `python -m` exits 0 with no output,
        which the watchdog would misread as healthy.
        """
        missing_path = tmp_path / "nope"
        result = subprocess.run(
            [sys.executable, "-m", "gridbot.status_check", "--path", str(missing_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert result.stdout.startswith("ALERT reason=missing")
        assert result.stdout.count("\n") == 1
        assert result.stderr == ""

        healthy_path = _write(
            tmp_path, "status.json", _snap("healthy", datetime.now(UTC).isoformat())
        )
        result = subprocess.run(
            [sys.executable, "-m", "gridbot.status_check", "--path", healthy_path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.startswith("OK")
        assert result.stdout.count("\n") == 1
        assert result.stderr == ""

    @pytest.mark.parametrize("bad_value", ["0", "-5", "nan", "inf"])
    def test_main_rejects_bad_max_age(self, tmp_path, bad_value, capsys):
        """Non-finite or non-positive --max-age-seconds is rejected by argparse (exit 2)."""
        path = _write(tmp_path, "status.json", _snap())
        with pytest.raises(SystemExit) as exc_info:
            main(["--path", path, "--max-age-seconds", bad_value])
        assert exc_info.value.code == 2

    def test_main_custom_max_age_reaches_check_status(self, tmp_path, monkeypatch):
        """--max-age-seconds is parsed and passed through to check_status."""
        recorded = {}

        def fake_check_status(path, *, now, max_age_seconds):
            recorded["max_age_seconds"] = max_age_seconds
            return StatusVerdict(StatusReason.OK, "healthy", 1.0, "")

        monkeypatch.setattr(status_check, "check_status", fake_check_status)
        rc = main(["--path", str(tmp_path / "s.json"), "--max-age-seconds", "15"])
        assert rc == 0
        assert recorded["max_age_seconds"] == 15.0

    def test_main_default_path(self, monkeypatch):
        """With no --path, argparse uses _DEFAULT_STATUS_PATH; verified without touching /tmp."""
        recorded = {}

        def fake_check_status(path, *, now, max_age_seconds):
            recorded["path"] = path
            recorded["max_age_seconds"] = max_age_seconds
            return StatusVerdict(StatusReason.OK, "healthy", 1.0, "")

        monkeypatch.setattr(status_check, "check_status", fake_check_status)
        rc = main([])
        assert rc == 0
        assert recorded["path"] == "/tmp/gridbot_status.json"
        assert recorded["max_age_seconds"] == 60.0
        assert (
            status_check._DEFAULT_STATUS_PATH
            == GridbotConfig.model_fields["status_file_path"].default
        )
