"""Tests for live_check.runner — per-strat ReplayConfig composition."""

from datetime import datetime, timedelta
from decimal import Decimal

from live_check.runner import build_replay_config
from live_check.window import Window


class TestBuildReplayConfig:
    def test_seed_and_follower_wiring(self, strat):
        """Seeded event_follower config with strat_id salt and account_id."""
        start = datetime(2026, 7, 1, 8, 0, 0)
        window = Window(start=start, end=start + timedelta(hours=4))
        config = build_replay_config(
            strat=strat,
            window=window,
            run_id="test-run-id",
            database_url="sqlite:///recorder.db",
            account_id="acc-uuid",
        )
        assert config.fill_simulator.mode == "event_follower"
        assert config.symbol == "LTCUSDT"
        assert config.run_id == "test-run-id"
        assert config.start_ts == window.start
        assert config.end_ts == window.end
        # 0080 salt + seed keyed to the window start.
        assert config.strategy.strat_id == "ltcusdt_test"
        assert config.seed.enabled is True
        assert config.seed.at_ts == window.start
        assert config.seed.strat_id == "ltcusdt_test"
        assert config.seed.account_id == "acc-uuid"

    def test_geometry_and_risk_mirror(self, strat):
        """All five risk fields + geometry project into the strategy config."""
        window = Window(
            start=datetime(2026, 7, 1, 8, 0, 0),
            end=datetime(2026, 7, 1, 12, 0, 0),
        )
        config = build_replay_config(
            strat=strat,
            window=window,
            run_id="r",
            database_url="sqlite:///x.db",
            account_id="a",
        )
        s = config.strategy
        assert s.tick_size == Decimal("0.1")
        assert s.grid_count == 20
        assert s.grid_step == 0.4
        assert s.amount == "x0.0005"
        assert s.max_margin == 5.0
        assert s.min_liq_ratio == 0.8
        assert s.max_liq_ratio == 1.2
        assert s.min_total_margin == 3.0
        assert s.increase_same_position_on_low_margin is True
        assert s.leverage == 10
        assert s.enable_risk_multipliers is True
