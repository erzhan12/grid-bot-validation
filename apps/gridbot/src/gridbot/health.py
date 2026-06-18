"""Operational observability: health-state snapshot + process-lifetime metrics.

Feature 0082 (issue #185). Additive, no trading-logic change. Counters are
incremented on the MAIN thread only (runner ticker / retry drain / orchestrator
sweep all run there), the same model as SafetyCaps / AuthCooldownManager — no
locking required. A snapshot is built once per ~10s health sweep and written to a
JSON status file; it COMPLEMENTS the gridbot-health CLI (it does NOT touch the
skill-owned health_state.json).
"""

import json
import logging
import os
from collections import defaultdict
from enum import StrEnum
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


class HealthState(StrEnum):
    """Overall bot health. ``str``-valued so it serializes directly to JSON."""

    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    AUTH_COOLDOWN = "auth_cooldown"
    CIRCUIT_OPEN = "circuit_open"


# Worst-wins precedence (higher index = worse) over the runtime states. STARTING
# is a pre-loop marker, not part of the comparison — the orchestrator sets it
# explicitly before the first sweep.
_PRECEDENCE = [
    HealthState.HEALTHY,
    HealthState.DEGRADED,
    HealthState.AUTH_COOLDOWN,
    HealthState.CIRCUIT_OPEN,
]


def worst_state(states: Iterable[HealthState]) -> HealthState:
    """Return the worst (highest-precedence) state; HEALTHY if none given.

    States outside the precedence ladder (e.g. STARTING, a pre-loop marker set
    only via ``overall=``) are ignored so a stray value can never raise.
    """
    worst = HealthState.HEALTHY
    for state in states:
        if state not in _PRECEDENCE:
            # A new enum value added without a precedence entry would land here;
            # warn so the gap is visible rather than silently down-ranked.
            logger.warning("Unknown health state ignored in precedence: %s", state)
            continue
        if _PRECEDENCE.index(state) > _PRECEDENCE.index(worst):
            worst = state
    return worst


class HealthMetrics:
    """Process-lifetime monotonic counters (reset only on restart).

    Inert by construction: a caller that holds no reference simply never
    increments. All methods are O(1) and main-thread-only.
    """

    def __init__(self) -> None:
        self.orders_placed = 0
        self.orders_placed_shadow = 0
        self.orders_rejected: dict[str, int] = defaultdict(int)
        self.cancels = 0
        self.cancels_failed = 0
        self.rest_errors_by_code: dict[str, int] = defaultdict(int)
        self.ws_reconnects: dict[str, int] = defaultdict(int)  # 'public' / 'private'

    def record_place(self, *, shadow: bool) -> None:
        if shadow:
            self.orders_placed_shadow += 1
        else:
            self.orders_placed += 1

    def record_reject(self, reason: str) -> None:
        self.orders_rejected[reason] += 1

    def record_cancel(self, *, success: bool) -> None:
        if success:
            self.cancels += 1
        else:
            self.cancels_failed += 1

    def record_rest_error(self, code: str) -> None:
        self.rest_errors_by_code[code] += 1

    def record_ws_reconnect(self, kind: str) -> None:
        self.ws_reconnects[kind] += 1

    def as_dict(self) -> dict:
        return {
            "orders_placed": self.orders_placed,
            "orders_placed_shadow": self.orders_placed_shadow,
            "orders_rejected": dict(self.orders_rejected),
            "cancels": self.cancels,
            "cancels_failed": self.cancels_failed,
            "rest_errors_by_code": dict(self.rest_errors_by_code),
            "ws_reconnects": dict(self.ws_reconnects),
        }


def build_snapshot(
    *,
    strat_states: list[dict],
    metrics: HealthMetrics,
    gauges: dict,
    generated_at: str,
    overall: Optional[HealthState] = None,
) -> dict:
    """Pure builder: assemble the status snapshot dict.

    ``strat_states`` is a list of per-strat dicts each carrying at least
    ``{"strat_id": str, "state": HealthState, "shadow": bool}``. ``overall`` lets
    the caller force a state (e.g. STARTING before the loop); otherwise the worst
    per-strat state wins. ``generated_at`` is supplied by the caller (UTC iso).
    """
    if overall is None:
        overall = worst_state(s["state"] for s in strat_states) if strat_states else HealthState.HEALTHY
    # str(...) coerces HealthState (a StrEnum) to a plain str — redundant for
    # json.dump (StrEnum serializes as its value) but explicit about the contract.
    return {
        "state": str(overall),
        "generated_at": generated_at,
        "strategies": [{**s, "state": str(s["state"])} for s in strat_states],
        "metrics": metrics.as_dict(),
        "gauges": gauges,
    }


class HealthStatusWriter:
    """Atomically write the health snapshot to a JSON status file.

    Replicates the ``GridStateStore._atomic_write`` tmp+flush+fsync+os.replace
    pattern (packages/gridcore/src/gridcore/persistence.py) against its own path —
    it does NOT call into GridStateStore. When ``enabled`` is False, ``write`` is a
    no-op (tests / standalone).
    """

    def __init__(self, status_file_path: str, enabled: bool = True) -> None:
        self._path = status_file_path
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def write(self, snapshot: dict) -> None:
        if not self._enabled:
            return
        dir_path = os.path.dirname(self._path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        tmp_path = self._path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(snapshot, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass
            raise
