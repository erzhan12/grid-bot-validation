"""One-way import adapter: trad_save_history -> replay-compatible SQLite.

Feature 0093. Imported DBs are for counterfactual A/B / relative ranking
only — the source has no private-stream data, so live-fidelity tooling
(event_follower / live_check) stays on recorder DBs.
"""
