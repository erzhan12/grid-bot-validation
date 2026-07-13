# Feature 0088 — live_check: External Review Trail

Plan: `docs/features/0088_PLAN.md` | Branch: `feature/0088-live-check`
Engines: codex CLI + cursor agent CLI (read-only), 3 rounds, 2026-07-13.

## Round 1 — 4 unique findings (2 engines overlapping)

| # | Sev | Finding | Verdict | Action |
|---|-----|---------|---------|--------|
| 1 | P2 | `--watch --last` override ignored — `watch_tick` recomputed window from `config.last` while `run_watch` used `args.last` only for the floor guard (both engines) | ACCEPT | `watch_tick` now takes an explicit resolved `last` timedelta from `run_watch`; regression test `test_watch.py::TestWatchWindowOverride` |
| 2 | P2 | `--per-fill` pairing broken on grain AND key: bt side is ONE `BacktestTrade` per order lifecycle (`_FillRollup`: VWAP/Σqty/Σpnl), and live `order_link_id` carries the `-{millis}` wire suffix that never equals the bt `client_order_id` prefix (both engines, verified at `runner.py:56-83,760-773`, `loader.py:113-125`) | ACCEPT | Rewritten: group live execs by `(extract_client_order_prefix(link) or order_id, order_id)`, compare group aggregates against the rollup trade; `ExecRow` gained `order_id`; tests: partial-fill ✓, multi-price VWAP ✓, aggregate mismatch ✗, NULL-link fallback |
| 3 | P2 | `load_config` search paths omit `apps/live_check/conf/` (codex) | REJECT | Mirrors `replay`/`recorder` cwd-relative convention exactly; plan pinned "mirroring replay.config.load_config" |
| 4 | P3 | `__pycache__`/`.pyc` artifacts under app dirs (codex) | REJECT | `git check-ignore` confirms ignored; not commit surface |

Cursor P3s round 1: exit-code tests + `build_replay_config` tests → ADDED (`TestExitCodes`, `test_runner.py`); heterogeneous `check_strat` tuples → accepted gap (simplicity); `read_only` SQLite-only no-op on Postgres → accepted gap (documented); empty-link `""` cross-pairing → fixed by the finding-2 rewrite (order_id fallback key).

## Round 2 — 1 valid P2 + P3s

| # | Sev | Finding | Verdict | Action |
|---|-----|---------|---------|--------|
| 1 | P2 | `make test` (the CI gate, `ci.yml:47`) never runs `apps/live_check/tests` — all 0088 tests CI-invisible (cursor) | ACCEPT | Added `uv run pytest apps/live_check/tests --cov=live_check --cov-append -q` to `Makefile`; RULES.md test list updated |
| 2 | P3 | Empty `strats: []` → `_exit_code([]) == 0` false-green (codex) | ACCEPT | `strats` now `min_length=1` (config error); `test_config.py` |
| 3 | P3 | Per-fill `ok` uses exact `==`, no $0.01 band (cursor) | REJECT | event_follower applies recorded exec price/qty/pnl verbatim — exact equality is the correct expectation; a band would mask real drift |
| 4 | P3 | `latest_ticker_ts` not window-scoped for `--once` (codex) | Accepted gap | Exec-present/no-tick window fails LOUDLY (live_only > 0 → FAIL), never false-green |
| 5 | P3 | Per-exec rows show group-aggregate bt columns (cursor) | Accepted gap | Documented in `render_per_fill` docstring — by-design grain |
| 6 | P3 | No CLI end-to-end `--once` exit-code test (cursor) | Accepted gap | `_exit_code` + `watch_tick` + `check_strat` covered at unit level |

## Round 3 — confirmation

- codex: `NO P1/P2 FINDINGS`, no P3s.
- cursor: `NO P1/P2 FINDINGS`; 1 P3 (pydantic `ValidationError` not caught by `main()`) → REJECT: `ValidationError` subclasses `ValueError` (verified: `issubclass(ValidationError, ValueError) is True`; `except (FileNotFoundError, ValueError)` catches it → clean `EXIT_FAIL`).

## Final status

- Findings: raised 13 unique, accepted 5 (all fixed), rejected 4 (evidence above), accepted-gap P3s 4.
- Tests: 84 live_check + full suite 2916 passed, 3 skipped.
- Lint: `ruff check .` clean.
- Result: SUCCESS (round 3 zero valid P1/P2).
