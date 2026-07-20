# Feature 0093 — apps/importer (trad_save_history → replay-compatible SQLite)

Plan: docs/features/0093_PLAN.md  |  Branch: feature/0093-importer

- [x] apps/importer package skeleton (pyproject, __init__)
- [x] config.py — CLI parsing, naive-UTC datetime discipline
- [x] source.py — fetch_batches protocol + factory
- [x] fetch_source_db.py — transport A (keyset pagination, read-only)
- [x] fetch_source_http.py — transport B (cursor pagination, retry/backoff)
- [x] mapping.py — row → TickerSnapshot (Decimal, NULL fallbacks)
- [x] output_db.py — WAL, parents, run row, per-batch commit, importlock
- [x] density.py — per-day counts, >60s gaps, LOW-DENSITY
- [x] validate.py — OHLC cross-check, smoke replay, recorder overlap probe
- [x] main.py — orchestration loop, aggregate exit code
- [x] conf/smoke_replay.yaml template
- [x] tests (8 files, 61 tests, all green)
- [x] workspace wiring: root pyproject pythonpath/testpaths, Makefile test line
- [x] make test (exit 0, TOTAL 90% ≥ 88 gate) + make lint green
- [x] /review-fix-loop-staged — 0 criticals; type hints/docstrings/validate tests folded in
- [x] /ext-code-review — 4 rounds codex+cursor, SUCCESS (trail: docs/features/0093_REVIEW.md); 86 tests, make test 91%, lint clean

## Plan deviations (documented)
- HTTP transport cannot probe MIN/MAX (contract has no endpoint) → explicit
  --start/--end required for --source http; clear ERROR otherwise.
- Added --recorder-db CLI arg for the --validate overlap probe (plan named the
  check but no arg); omitted → NOTICE skip.
- OHLC key-set check = imported-keys ⊆ kline-keys (extra imported keys hard-fail).
  Missing kline minutes are legitimate source sparsity.

## Not committed — awaiting user review/approval before any commit.
