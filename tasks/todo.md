# Feature 0110 Phase B3 — post-gap REST snapshot (#271 follow-up)

Plan: docs/features/0110_PLAN.md ("Phase B3 implementation notes")  |  Branch: feature/0110-b3-gap-resnapshot

- [x] recorder: `_schedule_post_gap_snapshot` at the end of `_recover_private_gap` (one in flight + one rerun; cancelled on stop)
- [x] recorder: `_snapshot_positions(evidence_only=True)` writes nothing for failed / empty / malformed symbols; labels in log lines
- [x] live-check: SKIP hint no longer says "restart the recorder" first
- [x] tests: fresh rows after gap_end, no placeholders, absent_side kept, coalescing, stop, lost-write gap; live-check interval moves past the gap
- [x] make test + make lint green; mutation checks (3/3 caught)
- [x] rules (recorder, live-check) + plan notes / Known limitations
- [ ] deploy the recorder on the VPS (after merge, with sign-off)

## Follow-ups
- PR #291 review P3: one long-lived REST client for post-gap snapshots (shared mainnet REST budget with the live gridbot during flapping).
- Orders after a gap: a post-gap open-orders resnapshot + marking DB-active orders absent from REST as gone would remove the `_order_resting_across_gap` SKIPs (part of #274).

# Feature 0110 Phase B2c — end-of-window anchor fitness (#271)

Plan: docs/features/0110_PLAN.md (Phase B2 steps 2, 3, 6, 7)  |  Branch: feature/0110-b2c-anchor-fitness

- [x] grid_db: `PositionSnapshotRepository.get_latest_received_before`; `OrderRepository.get_latest_by_order_ids`; wallet `get_by_account_range` id tie-break
- [x] ground_truth: `end_anchors`, `end_anchor_skip_reason` (placeholders, NULL unrealised, executions after anchor via orders join), end anchors in the coverage interval
- [x] ground_truth: `net_unrealised_per_pair` raises instead of a partial sum
- [x] main: end anchors looked up once in `_gate_skip_reason`, fitness after coverage
- [x] tests: fitness rules, audit port, positive path PASS/FAIL, all modes; existing tests get `fit_anchors`; carry-overs
- [x] make test + make lint green; mutation checks (4/4 caught)
- [x] rules (live-check, grid-db) + plan as-built notes and Known limitations

# Feature 0110 Phase B2b — live-check private-coverage gate (#271)

Plan: docs/features/0110_PLAN.md (Phase B2 steps 1, 3, 4 + lag floor)  |  Branch: feature/0110-b2b-coverage-gate

- [x] event_saver: public `PRIVATE_WS_HEALTH_CHECK_INTERVAL`; live_check depends on `event-saver` (+ uv.lock)
- [x] window: `parse_lag` rejects lag <= probe interval + LIVENESS_MARGIN (85 s), used by all three `run_*`
- [x] grid_db: `PrivateStreamSessionRepository.list_for_run`, `PrivateStreamGapRepository.list_overlapping`
- [x] ground_truth: `private_coverage_skip_reason` (tables, session/checkpoint, gaps; interval from seed rows)
- [x] main: gate in `run_single`, `run_shared_single`, `watch_tick`; conftest `private_coverage` fixture
- [x] make test + make lint green
- [x] rules (live-check, grid-db, event-saver) + plan as-built notes

## Follow-ups
- ~~PR #289 review P1: post-gap SKIP until a leg fills~~ — done in 0110 B3 (post-gap REST snapshot).

# Feature 0110 Phase B2a — recorder position-data fitness (#271)

Plan: docs/features/0110_PLAN.md (B2 split: B2a writer side, B2b coverage gate, B2c anchor fitness)  |  Branch: feature/0110-b2a-position-fitness

- [x] recorder: startup zero-rows carry `raw_json={"synthetic": "rest_failure" | "malformed" | "absent_side"}`
- [x] PositionWriter: `side=""` (flat hedge leg) → side from `positionIdx` (1 → Buy, 2 → Sell)
- [x] replay: `_seed_pre_check` docstring + `replay.md` — MIN(exchange_ts) may be a WS row since B1c-2
- [x] make test + make lint green
- [x] rules update (recorder, event-saver, replay) + plan as-built notes

## Follow-ups
- PR #288 review P1 (pre-existing, LIVE gridbot, out of scope for this
  recorder PR): `gridbot.position_fetcher.on_position_message` (and the
  REST fallback in `_fetch_one_account`) skip rows with `side == ""`.
  Bybit sends `side=""` for a hedge leg that closed to flat, so the WS
  cache keeps the pre-close dict and `runner.on_position_update` builds
  size / position_ratio / liq / C1 notional from a position that no longer
  exists until the leg reopens. Fix with positionIdx leg resolution (same
  rule as `event_saver.writers.leg_side`), update
  `test_skips_empty_symbol_or_side`, add a flat-leg-clears-slot test;
  needs its own plan, review and a VPS deploy.
- PR #288 review P2 (comparator): `PositionComparator._pair_side` takes
  the FIRST unclaimed live row inside the ±5 s window, not the nearest.
  Live per-side streams carry rows the backtest never emits (Bybit pushes
  on order create/amend/cancel, and since B2a flat-leg rows), so a
  backtest row can claim an extra live row and leave its true match
  unconsumed (spurious state_diverged, lost coverage). Pre-existing for
  non-flat extra rows; consider nearest-in-time pairing plus an
  asymmetric-stream test (live [open, flat, open] vs bt [open, open]).
- PR #288 review P2 (B2c input): decide whether ALL configured symbols
  returning `empty_response` should make the startup snapshot
  INCOMPLETE (mis-scoped key) — today it is WARNING + OK.
- PR #288 review P3: `PositionWriter` falls back from an empty
  `updatedTime` straight to `local_ts`; it could try the frame
  `creationTime` first like `wallet_writer._resolve_exchange_ts`
  (V5 position frames carry `updatedTime`, so low value).

# Feature 0110 Phase B1c-2 — confirmed startup (#271)

Plan: docs/features/0110_PLAN.md (B1 step 8 + step 1 startup part)  |  Branch: feature/0110-b1c2-startup

- [x] collectors: `CollectorStartError`; private `connect()` + `wait_ready` in one daemon thread, 10 s bound, not ready → disconnect + raise
- [x] collectors: public `connect()` bounded the same way
- [x] recorder: collectors first, session row + REST snapshot after a confirmed private start; start failure → `RECORDER_SNAPSHOT_INCOMPLETE` + raise
- [x] EventSaver: a not-ready account is logged and skipped, the others start
- [x] launcher: sentinel wait 15 → 60 s; signal handlers installed before `start()`
- [x] #286 follow-ups: public `LIVENESS_MARGIN`; lost write older than an open gap; `_open_gap_failures` reset on reconnect only; WARNING for open-gap failures after the 3rd
- [x] make test + make lint green
- [x] rules update (event-saver, recorder, grid-db) + plan as-built notes

## Follow-ups
- PR #287 review P2: standalone EventSaver drops an account whose private
  collector did not start, and nothing calls add_account() again, so a
  transient start failure disables that account for the process lifetime
  (before B1c-2 the health probe recovered it). Retry the start (bounded)
  before dropping, or keep a retry loop for skipped accounts.
- PR #287 review P3: EventSaver.add_account() registers only after
  start() returns, so two concurrent calls for the same account can open
  two sockets. Reserve the slot before awaiting the start.

# Feature 0110 Phase B1c-1 — private-stream coverage: sessions, open gaps, checkpoint (#271)

Plan: docs/features/0110_PLAN.md (B1 steps 3-5, 7; B1c split: B1c-1 coverage, B1c-2 startup)  |  Branch: feature/0110-b1c1-coverage

- [x] grid_db: `private_stream_sessions` model + repository (open, advance checkpoint never backward); gap `close_gap`
- [x] writers: execution/order/position/wallet `flush()` returns bool (empty → True, DB error → False, requeue kept)
- [x] collector: owner `on_disconnect(gap_start)` before reset (raise → skip reset); async `on_healthy_probe` (not while liveness-only); `is_degraded()`
- [x] recorder: open gap rows per symbol on disconnect (one txn), close on reconnect, retry failed close/outcome writes
- [x] recorder: pending-future registry + checkpoint barrier (await futures, retry writes, flush writers, publish barrier − 75 s)
- [x] recorder: session row after private start; `private_ws.degraded` in Health stats
- [x] make test + make lint green
- [x] rules update (grid-db, event-saver, recorder) + plan as-built notes

## Follow-ups
- 0110 B1c-1 local review: events dropped before a writer's buffer
  (collector normalizer / callback errors, an order with no `run_id`) are
  logged but invisible to the coverage checkpoint. Count them on the
  collector / writers and block the checkpoint (or open a gap) when any occur.
- PR #286 review P2: `_private_checkpoint` runs synchronous DB work on the
  event loop every healthy probe (`advance_checkpoint`, queued gap-write
  retries). Move it to `asyncio.to_thread`; the gap bookkeeping state is
  loop-only today, so that needs its own locking design.
- PR #286 review P3: a fallback gap row inserted on retry keeps
  `recovery_status='pending'` although its recovery finished; carry the
  outcome into the retried insert.

# Feature 0110 Phase B1b — private WS readiness + silent-reconnect detection (#271)

Plan: docs/features/0110_PLAN.md (B1b, detect-only)  |  Branch: feature/0110-b1b-collector-detect

- [x] ws_client: opt-in ack tracking, wait_ready, socket_identity, is_authenticated (gridbot untouched)
- [x] PrivateCollector: readiness at start and after reset, identity/auth health, gap start = last healthy − 75 s
- [x] not ready at start logs ERROR (no raise); not ready after reset keeps the gap open for the next probe
- [x] set_outcome scoped by run_id; reconciler warns per dropped REST row
- [x] make test + make lint green
- [x] rules update (bybit-adapter, event-saver, grid-db)

## Follow-ups
- PR #283 review P2: surface the liveness-only degraded state. Set a flag
  when `_MAX_UNREADY_RESETS` is hit (cleared on a successful readiness),
  expose it from `PrivateCollector`, and add it to `Recorder.get_stats()` so
  the periodic Health line shows it. Today one ERROR is the only signal that
  a topic (possibly `execution`) is never acked.
- PR #283 review P3: back off on a persistently unauthenticated socket
  (revoked/expired key). Each probe resets it and reports a ~90 s gap with a
  REST recovery, indefinitely; skip the reset and throttle the ERROR instead.

# Feature 0110 Phase B1a — gap persistence + structured recovery result (#271)

Plan: docs/features/0110_PLAN.md (B1 split into B1a/B1b/B1c)  |  Branch: feature/0110-b1a-recovery-persistence

- [x] grid_db: RecoveryStatus enum, PrivateStreamGap model (CASCADE to runs), PrivateStreamGapRepository
- [x] reconciler: ExecutionRecoveryResult; window [gap_start-5s, gap_end+5s]; 7-day guard; failed/truncated/skipped statuses
- [x] recorder: pending gap row per symbol + outcome done-callback; EventSaver logs outcome
- [x] prepare_session wipe clears gaps (CASCADE) test
- [x] make test + make lint green
- [x] rules update (event-saver, grid-db, recorder, core-invariants pitfall 24)
- [x] PR #282 review round 1: 7-day clamp keeps the tail, distinct-id duplicates, status round-trip test, callback type hints

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
