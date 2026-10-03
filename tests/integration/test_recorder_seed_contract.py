"""The recorder's post-gap evidence guard (0110 B3) must check what replay
seeds from and what live-check treats as unfit; the recorder cannot import
either, so the constants are pinned here."""

from live_check import ground_truth
from recorder import recorder
from replay.config import SeedConfig


class TestRecorderSeedContract:
    def test_wallet_seed_coin_matches_replay(self):
        """The recorder proves the coin replay seeds its wallet from."""
        assert (
            recorder._WALLET_SEED_COIN
            == SeedConfig.model_fields["wallet_coin"].default
        )

    def test_unproven_markers_match_live_check(self):
        """The rows the recorder will not write are those live-check rejects."""
        assert recorder._UNPROVEN_SYNTHETIC == ground_truth._UNFIT_SYNTHETIC
