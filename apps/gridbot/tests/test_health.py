"""Unit tests for gridbot.health (feature 0082 / issue #185)."""

import json

import pytest

from gridbot.health import (
    HealthMetrics,
    HealthState,
    HealthStatusWriter,
    build_snapshot,
    worst_state,
)


class TestWorstState:
    def test_empty_is_healthy(self):
        assert worst_state([]) == HealthState.HEALTHY

    def test_single(self):
        assert worst_state([HealthState.DEGRADED]) == HealthState.DEGRADED

    @pytest.mark.parametrize("states, expected", [
        ([HealthState.HEALTHY, HealthState.DEGRADED], HealthState.DEGRADED),
        ([HealthState.DEGRADED, HealthState.AUTH_COOLDOWN], HealthState.AUTH_COOLDOWN),
        ([HealthState.AUTH_COOLDOWN, HealthState.CIRCUIT_OPEN], HealthState.CIRCUIT_OPEN),
        ([HealthState.HEALTHY, HealthState.CIRCUIT_OPEN, HealthState.DEGRADED], HealthState.CIRCUIT_OPEN),
    ])
    def test_precedence_worst_wins(self, states, expected):
        assert worst_state(states) == expected

    def test_state_is_str_valued(self):
        assert HealthState.CIRCUIT_OPEN == "circuit_open"
        assert json.dumps({"s": HealthState.AUTH_COOLDOWN}) == '{"s": "auth_cooldown"}'


class TestHealthMetrics:
    def test_record_place_live_vs_shadow(self):
        m = HealthMetrics()
        m.record_place(shadow=False)
        m.record_place(shadow=True)
        m.record_place(shadow=True)
        assert m.orders_placed == 1
        assert m.orders_placed_shadow == 2

    def test_record_reject_by_reason(self):
        m = HealthMetrics()
        m.record_reject("insufficient_balance")
        m.record_reject("insufficient_balance")
        m.record_reject("auth")
        assert m.orders_rejected == {"insufficient_balance": 2, "auth": 1}

    def test_record_cancel(self):
        m = HealthMetrics()
        m.record_cancel(success=True)
        m.record_cancel(success=False)
        assert m.cancels == 1 and m.cancels_failed == 1

    def test_record_rest_error_and_ws(self):
        m = HealthMetrics()
        m.record_rest_error("110007")
        m.record_rest_error("110007")
        m.record_ws_reconnect("public")
        assert m.rest_errors_by_code == {"110007": 2}
        assert m.ws_reconnects == {"public": 1}

    def test_as_dict_is_json_serializable(self):
        m = HealthMetrics()
        m.record_place(shadow=False)
        m.record_reject("other")
        json.dumps(m.as_dict())  # must not raise


class TestBuildSnapshot:
    def _metrics(self):
        m = HealthMetrics()
        m.record_place(shadow=False)
        return m

    def test_overall_is_worst_strat_state(self):
        snap = build_snapshot(
            strat_states=[
                {"strat_id": "a", "state": HealthState.HEALTHY, "shadow": False},
                {"strat_id": "b", "state": HealthState.AUTH_COOLDOWN, "shadow": False},
            ],
            metrics=self._metrics(),
            gauges={"runners": 2},
            generated_at="2026-06-18T00:00:00Z",
        )
        assert snap["state"] == "auth_cooldown"
        # per-strat states serialize as plain strings
        assert snap["strategies"][1]["state"] == "auth_cooldown"
        assert snap["metrics"]["orders_placed"] == 1
        assert snap["gauges"]["runners"] == 2
        json.dumps(snap)  # fully serializable

    def test_overall_override(self):
        snap = build_snapshot(
            strat_states=[],
            metrics=self._metrics(),
            gauges={},
            generated_at="2026-06-18T00:00:00Z",
            overall=HealthState.STARTING,
        )
        assert snap["state"] == "starting"

    def test_no_strats_defaults_healthy(self):
        snap = build_snapshot(
            strat_states=[], metrics=self._metrics(), gauges={},
            generated_at="2026-06-18T00:00:00Z",
        )
        assert snap["state"] == "healthy"


class TestHealthStatusWriter:
    def test_writes_valid_json(self, tmp_path):
        path = str(tmp_path / "status.json")
        HealthStatusWriter(path).write({"state": "healthy", "n": 1})
        with open(path) as f:
            assert json.load(f) == {"state": "healthy", "n": 1}

    def test_disabled_is_noop(self, tmp_path):
        path = tmp_path / "status.json"
        HealthStatusWriter(str(path), enabled=False).write({"state": "healthy"})
        assert not path.exists()

    def test_atomic_no_tmp_left_on_failure(self, tmp_path, monkeypatch):
        path = str(tmp_path / "status.json")
        writer = HealthStatusWriter(path)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("os.replace", boom)
        with pytest.raises(OSError):
            writer.write({"state": "healthy"})
        # tmp file cleaned up; no garbage and no partial status file
        assert not (tmp_path / "status.json.tmp").exists()
        assert not (tmp_path / "status.json").exists()
