# Feature 0110 Phase A — REST execution recovery + unknown closed PnL (#271)

Plan: docs/features/0110_PLAN.md (Phase A only; B1/B2 deferred)  |  Branch: feature/0110-rest-exec-recovery

Cycle 1 — consumers reject unknown PnL (lands first):
- [x] grid_db.data_quality.RecordedDataQualityError
- [x] live_check ground_truth.sum_realized raises on NULL closed_pnl
- [x] replay engine + multi_engine event-follower loaders reject NULL
- [x] backtest RecordedExecution guard
- [x] comparator _aggregate_fills rejects NULL group
- [x] live_check render shows UNKNOWN, no positive flag
- [x] live_check once/watch/shared SKIP on unknown PnL before replay

Cycle 2 — producers emit NULL:
- [x] adapter get_executions validates result.category (non-empty list)
- [x] gridcore ExecutionEvent.closed_pnl Optional (default None)
- [x] normalizer: missing/empty PnL → None, explicit 0 kept
- [x] reconciler: no per-item category filter, REST PnL via same parser → NULL, raw_json
- [x] repository bulk_insert: predicated upsert enrich NULL→known, in-batch dedup (return stays int = inserted+enriched; see plan deviations)
- [x] callers: none needed (int return kept)
- [x] ported audit test green; make test + make lint


Phase A deviations from plan (recorded in 0110_PLAN.md "Phase A implementation notes"):
- bulk_insert keeps its int return (inserted+enriched); callers' len-count math already = unchanged
- live-check unknown-PnL SKIP lives in check_strat + run_shared_single (next to the empty-window guard), not inside freshness_skip_reason
- comparator CLI unchanged: existing logger.exception("Comparison failed") already surfaces the error
- cycles not committed separately (no commit without explicit ask)

Merged to main via PR #281 (4d3cdb0). Rules updated (f9b5580); recorder gap test uses the documented REST row shape and unknown_pnl_exec_ids has a scoping test (feature/0110-test-gaps). Open: Phases B1/B2.

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

# Feature 0098 — remote market-data API client protocol 1.0

Plan: docs/features/0098_PLAN.md

- [x] Phase 1: source-aware config preflight, secret loading, auth/session lifecycle
- [x] Phase 2: shared JSON request path, retries, Retry-After, redaction
- [x] Phase 3: bulk-page schema, damaged-row filtering, cursor/order invariants
- [x] Phase 4: authenticated kline fetch, strict minute/window validation
- [x] Phase 5: tests, CLIENT/README/rules updates, full verification

## Follow-ups
- 0098 review P2: optional charset warning in `_is_json_content_type` (warn on
  non-utf-8 charset parameters without rejecting otherwise-valid JSON).
- 0098 review P2: extra stress tests for concurrent lock acquisition and very
  large cursor pagination memory bounds (cycle set stays unbounded by design).
- 0098 review P3: consumer-side MARKET_DATA_API_KEY handling notes belong in
  importer README/ops docs, not the protocol `docs/CLIENT.md` contract.
- 0098 review round-2 P1/P2 deferred: `_safe_hostname` split refactor; hot-path
  timestamp filter micro-opts; live HTTP integration server test; cached
  `_headers()` dict; truncated output digests; preflight redirect note;
  `_validate_bulk_shape` module-level move; extra preflight_http comments;
  CLIENT.md ops rate-limit section.

# Feature 0102 — Account-wide liquidation halt

Plan: docs/features/0102_PLAN.md | Issue: #246

- [x] Phases 1–4: result fields, shared-pool observer, engine gate, docs.
- [x] Phase 5: required replay/gridcore/live-check tests, `make test`, and lint.

# Feature 0104 — Cancel failure handling

- [x] Phase 1: adapter structured cancellation result and classification (TDD)
- [x] Phase 2: executor error routing, cooldown, metrics, and retry handling (TDD)
- [x] Phase 3: loss-breaker cancellation alerting (TDD)
- [x] Required verification gates

# Feature 0107 — Recorder gap reconciliation tests

## Follow-ups
- 0107 review P2: add a recorder-level test where `get_recent_trades` /
  `get_executions_all` raises, and assert the recorder stays running and the
  error is logged via `_log_future_error`. Out of scope for #211 (the
  "marks run unhealthy" case is not current behavior).

# Research P23 — Double ATR opening distance (2026-09-27)

- [x] Freeze independent four-cell plan: results/double_atr_pilot_plan.md.
- [x] Phase 1: causal closed-bar ATR policy; 11 focused tests passed.
- [x] Phase 2: 8 calm/crash smokes, raw/compact log parity, fill-ledger checks;
  P22 static reproduced; make lint passed.
- [x] Phase 3: four full cells completed and independently reconciled;
  results/double_atr_pilot_results.md. P23 stopped by frozen criteria:
  double +0.2070% net / 29.1524% DD vs static +46.6494% / 33.1885%; no retuning.
