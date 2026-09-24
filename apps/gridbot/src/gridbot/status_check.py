"""Consumer contract for the health status file (feature 0109, issue #258 Phase 1).

Feature 0082 (`gridbot.health`) writes a JSON snapshot to `status_file_path`
(default `/tmp/gridbot_status.json`) roughly every 10 seconds. Nothing read it
until this module: a crashed / SIGKILLed / OOM-killed / wedged process leaves a
frozen file that nobody notices.

Contract (normative, mirrored in `.claude/rules/gridbot.md`): given a path, an
injected UTC "now", and a staleness threshold, evaluate in order — first
failure wins:

1. missing — the file does not exist.
2. malformed — unreadable, not valid UTF-8, not valid JSON, top-level is not a
   JSON object, or `state`/`generated_at` are absent/wrong-typed/unparseable
   (including a `generated_at` timestamp further in the future than the
   threshold allows, which a same-host clock cannot legitimately produce).
3. stale — the snapshot is older than `max_age_seconds`, regardless of the
   recorded `state` (a frozen `healthy` file is the primary failure mode this
   module exists to catch).
4. unhealthy — fresh, but `state` is `degraded` / `auth_cooldown` /
   `circuit_open`.
5. ok — fresh and `state` is `healthy` or `starting`.

Only `state` and `generated_at` are part of the contract; every other key is
ignored (forward-compatible with future producer fields).

Stdlib + `gridbot.health.HealthState` only — no pybit / DB / config import, so
a cron invocation (`python -m gridbot.status_check`) stays light. Alerts are
NOT sent from here: `gridbot.notifier.Notifier` uses a daemon background
thread that is killed at interpreter exit in a short-lived process, so this
module only returns an exit code and a one-line verdict; the VPS
`watchdog.sh` cron job (see `docs/deploy/status_watchdog.md`) calls this CLI
and routes a non-zero verdict through its own existing Telegram alerting.
"""

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, UTC
from enum import StrEnum
from typing import Optional

from gridbot.health import HealthState

_DEFAULT_STATUS_PATH = "/tmp/gridbot_status.json"
_DEFAULT_MAX_AGE_SECONDS = 60.0
_OK_STATES = {HealthState.HEALTHY, HealthState.STARTING}
_ALL_STATES = frozenset(HealthState)


class StatusReason(StrEnum):
    """Why `check_status` reached its verdict."""

    OK = "ok"
    MISSING = "missing"
    MALFORMED = "malformed"
    STALE = "stale"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class StatusVerdict:
    """Result of evaluating one status-file snapshot against the contract."""

    reason: StatusReason
    state: Optional[str]
    age_seconds: Optional[float]
    detail: str

    @property
    def healthy(self) -> bool:
        """True only for `StatusReason.OK`."""
        return self.reason is StatusReason.OK

    def format_line(self, path: str) -> str:
        """Render the one-line stdout verdict for the given `path`.

        `OK state=<s> age=<a>s path=<p>` when healthy, otherwise
        `ALERT reason=<r> state=<s|-> age=<a|->s path=<p> detail=<d>`.
        """
        if self.healthy:
            return f"OK state={self.state} age={self.age_seconds}s path={path}"
        state = self.state if self.state is not None else "-"
        age = self.age_seconds if self.age_seconds is not None else "-"
        return (
            f"ALERT reason={self.reason.value} state={state} age={age}s "
            f"path={path} detail={self.detail}"
        )


def check_status(path: str, *, now: datetime, max_age_seconds: float) -> StatusVerdict:
    """Evaluate the status file at `path` against the consumer contract.

    Pure: `now` is injected for hermetic tests. Never raises for bad file
    content — that is a verdict (`StatusReason.MALFORMED`), not an exception.
    Raises `ValueError` if `now` is naive or `max_age_seconds` is not a finite
    positive number.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not (math.isfinite(max_age_seconds) and max_age_seconds > 0):
        raise ValueError("max_age_seconds must be finite and > 0")

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return StatusVerdict(StatusReason.MISSING, None, None, "status file not found")
    except OSError as exc:
        return StatusVerdict(
            StatusReason.MALFORMED, None, None, f"status file unreadable: {exc}"
        )

    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        return StatusVerdict(
            StatusReason.MALFORMED, None, None, f"status file content invalid: {exc}"
        )

    if not isinstance(doc, dict):
        return StatusVerdict(
            StatusReason.MALFORMED, None, None, "status file top-level JSON is not an object"
        )

    generated_at_raw = doc.get("generated_at")
    if not isinstance(generated_at_raw, str):
        return StatusVerdict(
            StatusReason.MALFORMED, None, None, "generated_at missing or not a string"
        )
    try:
        generated_at = datetime.fromisoformat(generated_at_raw)
    except ValueError:
        return StatusVerdict(
            StatusReason.MALFORMED, None, None, "generated_at not parseable"
        )
    if generated_at.tzinfo is None:
        return StatusVerdict(
            StatusReason.MALFORMED, None, None, "generated_at is naive (missing tzinfo)"
        )

    state = doc.get("state")
    if not isinstance(state, str) or state not in _ALL_STATES:
        detail_state = state if isinstance(state, str) else None
        return StatusVerdict(
            StatusReason.MALFORMED,
            detail_state,
            None,
            "state missing or not a recognized HealthState value",
        )

    age_seconds = (now - generated_at).total_seconds()
    if age_seconds < -max_age_seconds:
        return StatusVerdict(
            StatusReason.MALFORMED,
            state,
            age_seconds,
            "generated_at is in the future beyond max_age_seconds",
        )

    if age_seconds > max_age_seconds:
        return StatusVerdict(
            StatusReason.STALE, state, age_seconds, "snapshot is older than max_age_seconds"
        )

    if state not in _OK_STATES:
        return StatusVerdict(
            StatusReason.UNHEALTHY, state, age_seconds, f"state={state!r} is not healthy"
        )

    return StatusVerdict(StatusReason.OK, state, age_seconds, "")


def _positive_finite_float(value: str) -> float:
    """argparse `type=` helper: parse a float, rejecting non-finite or <= 0."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from exc
    if not (math.isfinite(parsed) and parsed > 0):
        raise argparse.ArgumentTypeError(
            f"max-age-seconds must be finite and > 0, got {value!r}"
        )
    return parsed


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: print one verdict line, return 0 if healthy else 1."""
    parser = argparse.ArgumentParser(
        prog="python -m gridbot.status_check",
        description=(
            "Check the gridbot health status file for staleness/unhealthy state "
            "(feature 0109, issue #258 Phase 1)."
        ),
    )
    parser.add_argument("--path", default=_DEFAULT_STATUS_PATH)
    parser.add_argument(
        "--max-age-seconds",
        type=_positive_finite_float,
        default=_DEFAULT_MAX_AGE_SECONDS,
    )
    args = parser.parse_args(argv)

    verdict = check_status(
        args.path, now=datetime.now(UTC), max_age_seconds=args.max_age_seconds
    )
    print(verdict.format_line(args.path))
    return 0 if verdict.healthy else 1


if __name__ == "__main__":
    sys.exit(main())
