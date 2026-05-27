"""Tests for scripts/phase4/lib/recorder_snapshot_check.sh.

The classifier ``_classify_recorder_snapshot`` is sourced directly from bash
(no top-level side effects in the lib file). We do NOT source
``start_recorder.sh`` itself — that file has top-level side effects (cd,
pkill, prepare_session, rm log) starting at line 23.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_PATH = REPO_ROOT / "scripts" / "phase4" / "lib" / "recorder_snapshot_check.sh"
STOP_LIB_PATH = REPO_ROOT / "scripts" / "phase4" / "lib" / "recorder_stop.sh"


def _run_classifier(log_path: Path) -> subprocess.CompletedProcess[str]:
    """Source the lib in a fresh bash subprocess and call the classifier.

    Pass the log path as ``$1`` (double-quoted so it expands in the bash -c
    body). Do NOT single-quote ``"$LOG_FILE"`` — that prevents expansion.
    """
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{LIB_PATH}" && _classify_recorder_snapshot "$1"',
            "bash",
            str(log_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


class TestClassifierIncomplete:
    def test_incomplete_production_order(self, tmp_path: Path) -> None:
        """INFO line with zero counts → WARNING incomplete → sentinel.

        Mirrors recorder.py:308-326 production ordering on the failure path:
        the INFO line is emitted first with the zero counts, then the human
        WARNING, then the terminal RECORDER_SNAPSHOT_INCOMPLETE sentinel.
        """
        log = tmp_path / "recorder.log"
        log.write_text(
            "2026-05-27 16:00:00 INFO Initial REST snapshot: wallet=0 coins, positions=0 rows, open_orders=0\n"
            "2026-05-27 16:00:00 WARNING Initial REST snapshot incomplete: wallet_rows=0, position_rows=0 ...\n"
            "2026-05-27 16:00:00 WARNING RECORDER_SNAPSHOT_INCOMPLETE\n"
        )

        result = _run_classifier(log)

        assert result.returncode == 1, (
            f"expected rc=1 on incomplete; got {result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "Initial REST snapshot incomplete" in result.stderr
        assert "RECORDER_SNAPSHOT_INCOMPLETE" in result.stderr
        assert "Recorder PID:" not in result.stdout
        assert "Recorder PID:" not in result.stderr

    def test_auth_failure_only_sentinel(self, tmp_path: Path) -> None:
        """Auth-client construction failure path: ERROR + sentinel, no INFO line."""
        log = tmp_path / "recorder.log"
        log.write_text(
            "2026-05-27 16:00:00 ERROR Failed to construct authenticated REST client for initial snapshot: bad key\n"
            "2026-05-27 16:00:00 WARNING RECORDER_SNAPSHOT_INCOMPLETE\n"
        )

        result = _run_classifier(log)

        assert result.returncode == 1
        assert "RECORDER_SNAPSHOT_INCOMPLETE" in result.stderr


class TestClassifierSuccess:
    def test_success(self, tmp_path: Path) -> None:
        """Non-zero counts → INFO line → RECORDER_SNAPSHOT_OK."""
        log = tmp_path / "recorder.log"
        log.write_text(
            "2026-05-27 16:00:00 INFO Initial REST snapshot: wallet=1 coins, positions=2 rows, open_orders=40\n"
            "2026-05-27 16:00:00 INFO RECORDER_SNAPSHOT_OK\n"
        )

        result = _run_classifier(log)

        assert result.returncode == 0, (
            f"expected rc=0 on success; got {result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "Initial REST snapshot:" in result.stdout
        assert "RECORDER_SNAPSHOT_OK" in result.stdout
        # Classifier itself never prints the operator tail.
        assert "Recorder PID:" not in result.stdout


class TestClassifierTimeout:
    def test_no_sentinel(self, tmp_path: Path) -> None:
        """Log contains unrelated content only → rc=2."""
        log = tmp_path / "recorder.log"
        log.write_text(
            "2026-05-27 16:00:00 INFO public ws connected\n"
            "2026-05-27 16:00:00 INFO subscribed to publicTrade.BTCUSDT\n"
        )

        result = _run_classifier(log)

        assert result.returncode == 2, (
            f"expected rc=2 on timeout; got {result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "no RECORDER_SNAPSHOT_OK/INCOMPLETE" in result.stderr


class TestClassifierRaceGuard:
    def test_info_without_sentinel_is_timeout(self, tmp_path: Path) -> None:
        """INFO line with zero counts but no sentinel yet → rc=2 (NOT 0).

        Documents why the wait loop must not break on the human-readable INFO
        line: when wallet_count==0, _write_initial_rest_snapshot emits the
        INFO line BEFORE the WARNING and the INCOMPLETE sentinel. A loop
        that exits on the INFO line could classify a failing run as success
        if it then grepped for "Initial REST snapshot:" as a success signal.
        """
        log = tmp_path / "recorder.log"
        log.write_text(
            "2026-05-27 16:00:00 INFO Initial REST snapshot: wallet=0 coins, positions=2 rows, open_orders=0\n"
        )

        result = _run_classifier(log)

        assert result.returncode == 2, (
            "INFO line alone (no sentinel) must classify as timeout, "
            f"not success; got rc={result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )


@pytest.fixture(autouse=True)
def _lib_exists() -> None:
    if not LIB_PATH.exists():
        pytest.fail(f"classifier lib missing at {LIB_PATH}")
    if not STOP_LIB_PATH.exists():
        pytest.fail(f"stop helper lib missing at {STOP_LIB_PATH}")


def _run_stop(
    *,
    pkill_rc: int = 0,
    pgrep_sequence: list[int],
    wait_seconds: int = 2,
    stub_ps_line: str = "user 1234 0.0 0.0 1000 100 ? S 10:00 0:01 recorder --config fake-pattern",
) -> subprocess.CompletedProcess[str]:
    """Source recorder_stop.sh in a fresh bash subshell with stubbed
    pkill/pgrep/sleep/ps and invoke ``_stop_recorder_pattern``.

    ``pgrep_sequence`` is the list of return codes pgrep will yield on
    successive calls (0 = match, 1 = no match). The stub uses a counter file
    in ``/tmp`` so state survives across the subshell's pgrep invocations.

    ``sleep`` is stubbed to no-op so the loop runs instantly regardless of
    ``wait_seconds``.
    """
    import tempfile

    counter_file = tempfile.NamedTemporaryFile(
        prefix="pgrep_counter_", suffix=".txt", delete=False
    )
    counter_file.write(b"0")
    counter_file.close()

    # Bash array literal of pgrep return codes.
    seq_literal = " ".join(str(rc) for rc in pgrep_sequence)

    script = f"""
        pgrep_returns=({seq_literal})
        pkill() {{ return {pkill_rc}; }}
        pgrep() {{
          local n
          n=$(cat "{counter_file.name}")
          echo $((n + 1)) > "{counter_file.name}"
          local rc=${{pgrep_returns[$n]:-1}}
          return "$rc"
        }}
        sleep() {{ return 0; }}
        ps() {{ printf '%s\\n' "{stub_ps_line}"; }}
        source "{STOP_LIB_PATH}"
        _stop_recorder_pattern "fake-pattern" {wait_seconds}
    """
    try:
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(counter_file.name).unlink(missing_ok=True)


class TestStopRecorderPattern:
    """Tests for scripts/phase4/lib/recorder_stop.sh:_stop_recorder_pattern.

    These tests cover the launcher process-management branches that were
    previously untested — the gap that allowed the rc=2 timeout-without-kill
    bug to ship. Stub pkill/pgrep/sleep/ps via bash functions defined before
    the lib is sourced (bash function lookup takes precedence over PATH).
    """

    def test_already_stopped(self) -> None:
        """pgrep returns 1 on first probe → rc=0, no diagnostic emitted."""
        result = _run_stop(pgrep_sequence=[1])

        assert result.returncode == 0, (
            f"expected rc=0 when pattern already gone; got {result.returncode}\n"
            f"stderr={result.stderr!r}"
        )
        # Helper must NOT print a diagnostic when the kill succeeded.
        assert "recorder --config" not in result.stderr

    def test_stops_after_a_few_iterations(self) -> None:
        """pgrep matches on first two probes, then misses → rc=0 cleanly."""
        result = _run_stop(pgrep_sequence=[0, 0, 1], wait_seconds=5)

        assert result.returncode == 0, (
            f"expected clean stop after iterations; got {result.returncode}\n"
            f"stderr={result.stderr!r}"
        )
        assert "recorder --config" not in result.stderr

    def test_still_alive_after_wait(self) -> None:
        """pgrep keeps matching past wait_seconds → rc=1, diagnostic ps line emitted."""
        # Three iterations + one post-loop probe — all match. wait_seconds=3
        # so the loop runs 3 times then re-probes.
        result = _run_stop(pgrep_sequence=[0, 0, 0, 0], wait_seconds=3)

        assert result.returncode == 1, (
            f"expected rc=1 when pattern still matches; got {result.returncode}\n"
            f"stderr={result.stderr!r}"
        )
        # Diagnostic ps line must reach stderr so the operator sees what
        # is still running.
        assert "recorder --config" in result.stderr

    def test_pkill_rc1_treated_as_success(self) -> None:
        """pkill returns 1 (nothing matched) → not an error; loop still runs."""
        # `pkill -f` returns 1 when no process matched. The helper suppresses
        # that with `|| true`. We still need pgrep to confirm absence.
        result = _run_stop(pkill_rc=1, pgrep_sequence=[1])

        assert result.returncode == 0, (
            f"pkill rc=1 (no match) must not fail the helper; got {result.returncode}"
        )


class TestStartRecorderLauncherIntegration:
    """Sanity checks that start_recorder.sh sources both lib files and
    exposes the helper functions in the same shell.

    We do NOT source start_recorder.sh itself (top-level side effects); we
    re-source the same two libs from the same paths and assert both
    functions are defined and callable.
    """

    def test_both_libs_source_and_export_functions(self) -> None:
        script = f"""
            source "{LIB_PATH}"
            source "{STOP_LIB_PATH}"
            declare -F _classify_recorder_snapshot > /dev/null \\
              && declare -F _stop_recorder_pattern > /dev/null
        """
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, (
            f"both libs must source cleanly and define their functions;\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    def test_start_recorder_sources_stop_lib(self) -> None:
        """start_recorder.sh must source recorder_stop.sh — otherwise the
        helper calls inside the launcher will fail with 'command not found'.
        """
        launcher = REPO_ROOT / "scripts" / "phase4" / "start_recorder.sh"
        content = launcher.read_text()
        assert "lib/recorder_stop.sh" in content, (
            f"start_recorder.sh must source lib/recorder_stop.sh; not found"
        )
        assert "_stop_recorder_pattern" in content, (
            f"start_recorder.sh must call _stop_recorder_pattern"
        )
