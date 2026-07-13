# Feature 0088 — live_check app

Plan: docs/features/0088_PLAN.md  |  Branch: feature/0088-live-check

- [x] Phase 1: apps/live_check pyproject + root pyproject registration + config.py + conf/live_check.yaml
- [x] Phase 1B(a): grid_db read-only mode (settings.read_only, mode=ro URL rewrite, get_readonly_session) + tests
- [x] Phase 1B(b): ReplayEngine disable-snapshot-emission flag + no-op writer + tests
- [x] Phase 2A: runner.py (build_replay_config, run_strat)
- [x] Phase 2B: ground_truth.py (sums, net unrealised, exec count, ticker freshness, run start)
- [x] Phase 2C: verdict.py (Verdict dataclass, evaluate)
- [x] Phase 2D: render.py (once, watch_line, per_fill, curve)
- [x] Phase 3: main.py CLI (modes, window, guards, freshness, exit codes)
- [x] Phase 4: full test suite green (2907 passed, 3 skipped; 75 new live_check tests)
- [x] Lint clean (ruff)
- [x] RULES.md live_check section
- [x] ext-code-review (codex+cursor, 3 rounds): 5 accepted findings fixed — watch --last threading, per-fill rollup/prefix pairing, Makefile CI gate, strats min_length=1, extra tests; trail in docs/features/0088_REVIEW.md

## Not committed — awaiting user review/approval before any commit.
