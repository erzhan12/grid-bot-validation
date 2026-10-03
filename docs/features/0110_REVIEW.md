# 0110 Review — Phase A (REST execution recovery + unknown closed PnL, #271)

Scope: Phase A of `docs/features/0110_PLAN.md` only. Phases B1/B2 are deferred.

## External review trail (ext-code-review, 2026-09-29)

Engines: codex (`gpt-5.6-sol`, high) + cursor agent (read-only, `--mode ask`). Rounds: 2/4. Result: SUCCESS.

### Round 1

| # | Engine | Sev | Finding | Verdict |
|---|---|---|---|---|
| 1 | codex | P2 | `ground_truth.sum_realized` ran the NULL check and the `SUM` as two queries; a NULL row committed between them would be silently skipped by `SUM`. | ACCEPT — now one aggregate statement (`COALESCE(SUM(closed_pnl), 0)`, `COUNT(*) − COUNT(closed_pnl)`); raises on any NULL. |
| 2 | codex | P3 | `normalizer.py` docstring example still showed `closedPnl` for the WS payload. | ACCEPT — example uses the documented `execPnl`. |
| 3 | codex | P3 | Defensive `RecordedDataQualityError` handlers around replay/`collect` untested. | ACCEPT — `test_late_unknown_pnl_during_replay_renders_skip` (watch) and `test_shared_late_unknown_pnl_during_replay_skips` (shared). |
| 4 | cursor | P3 | `test_bulk_insert_skips_duplicates` docstring still said `ON CONFLICT DO NOTHING`. | ACCEPT — docstring updated. |
| 5 | cursor | P3 | `check_strat` return docstring omitted the unknown-PnL / data-quality SKIPs. | ACCEPT — docstring updated. |

Cursor: NO P1/P2 FINDINGS (with verification log).

### Round 2

| # | Engine | Sev | Finding | Verdict |
|---|---|---|---|---|
| 1 | codex | P3 | PostgreSQL branch of the predicated upsert has no Postgres execution/compile test. | ACCEPTED GAP — no Postgres in the test infrastructure; the previous `DO NOTHING` Postgres branch was equally untested. |
| 2 | cursor | P3 | REST SQLite round-trip test did not assert `order_id`, `order_link_id`, `symbol`, `raw_json["closedSize"]`. | ACCEPT — assertions added. |

Both engines: NO P1/P2 FINDINGS.

### Totals

Raised 7 (P2: 1, P3: 6); accepted 6 (fixed 6); rejected 0; P3 accepted gap 1.

## Local staged review (review-fix-loop-staged, 2026-09-29)

5 parallel reviewers (quality, security, performance, testing, docs). 0 CRITICAL → Ready to commit (1/3 iterations). Warnings fixed: `get_executions` `Raises:` documents the category `ValueError`; live-check exit-code docstring lists the unknown-PnL SKIP; reverse-order in-batch dedupe test; `collect()`-raises-after-replay test. Cheap INFO fixed: enrichment upsert `WHERE` also requires `account_id == excluded.account_id` (+ test); `LiveTradeLoader.load` / `ground_truth.collect` / replay `event_follower` docs; UNKNOWN on the unmatched render branch. INFO left as accepted: shared window-filter helper, `live_pnl` rename, reconciler bad-envelope test, `raw_json` key allow-list, Postgres row-lock note.

## External review — pass 2 (after local review)

Engines: codex (`gpt-5.6-sol`, high) + cursor. Rounds: 1/4. Result: SUCCESS.

| # | Engine | Sev | Finding | Verdict |
|---|---|---|---|---|
| 1 | cursor | P3 | `test_rest_client.py` envelope lines touched by this change exceeded 88 chars. | ACCEPT — wrapped. |

codex: NO P1/P2 FINDINGS, no P3. cursor: NO P1/P2 FINDINGS (with verification log).

## PR #281 claude-review (round 1)

Verdict APPROVED. Triage:
- **[P1] REST opening fills marked unknown** — ACCEPT, reclassified P2 (coverage, fail-closed; no false PASS). Verified: REST rows carry `closedSize`; Bybit `execPnl` is the PnL of a *close* (= `cashFlow`, gross), so `closedSize == 0` ⇒ realized PnL exactly 0. Fixed in `reconciler._rest_exec_pnl` (+ 6-case test). Benefit is narrow: a gap with any closing fill still SKIPs, and after Phase B2 any window overlapping a gap SKIPs regardless.
- [P2] per-fill `UNKNOWN` unreachable via CLI (window SKIPs first) — open, needs a user choice (render despite unknowns vs delete the dead branch).
- [P2 ×3] `.claude/rules/` updates (grid-db, bybit-adapter, replay, live-check) — deferred to workflow step 6 (after user verification).
- [P2] `RecordedExecution.closed_pnl` annotation `Optional[Decimal]` — open (trivial).
- [P3] validate `result.category` on empty pages too — not applied: Bybit's empty-page shape is unverified (deliberate choice, plan GREEN step 1).

## PR #281 claude-review (round 2, on 658c606)

Verdict APPROVED, no P0. Triage:
- **[P1] malformed `closedSize` drops the whole recovered row** — ACCEPT. The new `Decimal(str(closedSize))` parse raised inside `_executions_to_models`' per-row `except Exception` → row skipped (fail-open: a missing fill). Fixed: parse errors degrade to `None` (unknown); test case `closedSize="n/a"` keeps the row.
- [P2] `RecordedExecution.closed_pnl: Optional[Decimal]` — applied.
- [P3] `get_executions_all` `Raises:` documents the propagated `ValueError` — applied.
- [P3] reconciler `except → return 0` commented as a known fail-open window closed by Phase B1 — applied.
- [P3] enrichment keeps `raw_json` (origin payload) — commented as deliberate.
- [P2 ×4] `.claude/rules/` updates — still deferred to the post-verification rules step.

## PR #281 claude-review (round 3, on d103445)

Verdict APPROVED, no P0. Triage:
- **[P1] unguarded `parse_exec_pnl` in `_rest_exec_pnl`** — ACCEPT (pre-existing: the old reconciler also parsed `closedPnl` unguarded; REST rows carry no PnL field, so near-zero likelihood). Guarded for consistency: any PnL parse error → `None`; test case `execPnl="n/a"`.
- [P2] WARNING at WS ingest when a Trade row's PnL is unknown — applied (the post-deploy watch signal).
- [P3] blank line in `test_writers.py` — applied.
- Held: [P2] `closedSize == 0` inference for WS rows (Bybit documents `execPnl` on WS; revisit only if the ingest warning fires); [P2] Postgres compile test (accepted gap); [P3] dropped-row summary log (Phase B1 structured recovery result); [P2] `.claude/rules/` updates (post-verification rules step).
- Loop stopped here: each round now surfaces a fresh smaller item rather than a real defect.

### Final verification

- `make test`: exit 0, merged coverage 91% (gridcore 94.8%).
- `make lint`: all checks passed.
- Shipped on PR #281 (feature/0110-rest-exec-recovery); merge awaits explicit user approval.

---

# 0110 Review — Phase B1a (gap persistence + structured recovery result)

## Local staged review (review-fix-loop-staged, 2026-09-29)

5 reviewers, 0 CRITICAL → Ready to commit (1/3). Warnings fixed: shared `_is_trade_row` filter and conversion inside the `try` (reconcile never raises); `exc_info` on error logs; single `recovery_result_from_future` for recorder + EventSaver; `ExecutionRecoveryResult` `Attributes:` docstring; tests for gap-row write failure, outcome-write failure, cancelled/crashed futures, EventSaver logging, non-Trade rows, the exact 7-day boundary. Info fixed: `reason` capped at 500 chars, missing-row `ValueError` test, FAILED enum comment, docstrings, PEP 8 spacing, repository-location note. Left: defensive `run_id is None` guard, redundant `str()` / explicit zero defaults.

## External review (ext-code-review)

Engines: codex (`gpt-5.6-sol`, high) + cursor. Rounds 3/4. Result: SUCCESS. Cursor dropped after two consecutive `resource_exhausted` failures (rounds 2–3).

| Round | Engine | Sev | Finding | Verdict |
|---|---|---|---|---|
| 1 | codex | P2 | Non-dict REST rows silently filtered → `RECOVERED`. | ACCEPT — counted as dropped rows (`FAILED`, valid rows kept). |
| 1 | codex | P3 | Commit-time failure (session exit) untested. | ACCEPT — `test_commit_error_is_failed`. |
| 1 | cursor | P2 | Cancelled recovery stays `pending`: `_log_future_error`'s `future.exception()` raises and stops later callbacks. | REJECT — on Python 3.12 `concurrent.futures.CancelledError` subclasses `Exception` and `Future._invoke_callbacks` catches it and continues; pinned by `test_cancelled_recovery_is_persisted_failed_after_log_callback`. |
| 2 | codex | P2 | (a) dict rows without `execType` filtered as non-Trade; (b) Trade rows missing `execId`/`symbol`/`execTime` stored with empty/epoch defaults. | (a) ACCEPT — counted as dropped. (b) REJECT — pre-existing conversion defaults (unchanged since before 0110); Bybit documents the fields as always present; out of scope. |
| 2–3 | codex | P3 | All five statuses not persisted via a fresh session. | Accepted gap. |

## Final verification (B1a)

- `make test`: exit 0, merged coverage 91%.
- `make lint`: all checks passed.

## Phase B1b — local staged review (review-fix-loop-staged, 2026-09-29)

5 reviewers, 0 CRITICAL → Ready to commit (1/3). Warnings fixed: four pre-B1b dead-socket tests built a collector with `_ready=False` and never reached the liveness check (now `_mark_ready`, plus an assertion that `is_socket_alive` runs); new tests for `reset()` re-installing ack tracking, a `success: false` ack, an ack arriving mid-wait, the baseline read before the wait, and not-ready-at-start dated from the connect time; the gap outcome uses the `run_id` captured when the callback is built; `_is_malformed_row` helper; `_READY_WAIT_SLACK`; stale docstrings. pybit 5.13.0 internals checked and pinned in `bybit-adapter.md`. Bybit's private subscribe ack `req_id`: docs ambiguous; gocryptotrader matches `/v5/private` acks by `req_id` (only `/v5/trade` omits it). Documented, not changed: a never-ready socket resets every probe with no backoff; `connect()` still on the loop (B1c); `_is_ready` reads without the lock (race unreachable today).

## Phase B1b — external review (ext-code-review)

Engines: codex (`gpt-5.6-sol`, high) + cursor (`grok-4.7-high`). Rounds 2/4. Result: SUCCESS.

| Round | Engine | Sev | Finding | Verdict |
|---|---|---|---|---|
| 1 | codex | P2 | `stop()` awaits the health task, which can sit in `_confirm_ready` for up to 6 s; the plan's blocked-auth shutdown test is missing. | ACCEPT — `_confirm_ready` races `_ws_health_stop_event` and abandons the wait thread; not-ready ERROR skipped when stopping; `test_auth_wait_keeps_shutdown_responsive` (RED 4.9 s → GREEN). The test times `stop()` because `stop()` swallows `CancelledError`, so a `wait_for` would hide the delay. |
| 1 | cursor | — | NO P1/P2. | — |
| 1 | cursor | P3 | The `start()` wait is not ended by `stop()`. | ACCEPT (docs) — docstring says the start wait is bounded by the timeout only. |
| 1 | cursor | P3 | `wait_ready` docstring line > 88 chars. | ACCEPT — re-wrapped. |
| 1 | cursor | P3 | Unauthenticated / never-ready tests assert only `reset()`. | Accepted gap (gap path covered by other tests). |
| 2 | codex | — | NO P1/P2, no P3. | — |
| 2 | cursor | P3 | `set(self._acked_req_ids)` can raise while pybit adds. | REJECT — CPython copies a set in one step under the GIL. |
| 2 | cursor | P3 | Conversion-error drop warning untested. | REJECT — `test_row_conversion_error_is_failed_but_good_rows_kept` asserts `execId='bad'` in the log. |
| 2 | cursor | P3 | Gap-start test never runs a second probe after the baseline retake. | Accepted gap. |

## Final verification (B1b)

- `make test`: exit 0, merged coverage 92%.
- `make lint`: all checks passed.

## Phase B1c-1 — local staged review (review-fix-loop-staged, 2026-09-30)

5 reviewers, 2 iterations. 2 CRITICAL found and fixed → Ready to commit.

| Sev | Finding | Fix |
|---|---|---|
| CRITICAL | A close-gap write that failed stayed queued with the row's id still marked open; a second outage reused the row, and the queued close then replayed its older `gap_end` over it — the checkpoint advanced across an outage. Found by two reviewers, reproduced by one. | `_handle_private_gap` forgets the open id at once, so each outage owns its row (`test_stale_close_retry_does_not_touch_later_outage`). |
| CRITICAL | Healthy probes on a liveness-only socket still stamped the last-healthy time, so after recovery the gap started only 75 s back and the degraded stretch was certified. | Those probes stamp nothing (`test_liveness_only_period_is_not_certified_on_recovery`). |
| WARNING | Queued gap writes were retried only when the checkpoint got that far (not while degraded / after a lost write). | Retried at the top of every checkpoint attempt and on every private gap. |
| WARNING | The fallback gap row (late readiness) was best-effort: a DB error left the stretch certified with no row. | The failed insert is queued. |
| WARNING | A traceback on every retry while the DB is down. | First failure logs the traceback, retries one WARNING line. |
| WARNING | Test gaps: healthy-probe wiring, disconnect idempotency, multi-symbol all-or-nothing, outcome-write retry, session-open failure, barrier ordering, degraded stats. | Tests added. |
| WARNING | Stale rule text, `RULES.md` index, "confirmed session" wording, `add_gap` / `flush()` docstrings. | Fixed. |

Also added: the checkpoint refuses to advance while any gap row is open and re-checks the retry queue right before publishing. Second pass: no CRITICAL / WARNING. Deferred to `tasks/todo.md`: events dropped before a writer's buffer are invisible to the checkpoint; the checkpoint awaits pending writes with no timeout.

## Phase B1c-1 — external review (ext-code-review)

Engines: codex (`gpt-5.6-sol`, high) + cursor (`grok-4.7-high`). Rounds 3/4. Result: SUCCESS.

| Round | Engine | Sev | Finding | Verdict |
|---|---|---|---|---|
| 1 | codex | P1 | `_forget_pending` set the write-lost latch after releasing `_pending_lock`; a barrier in between saw neither the future nor the latch. | ACCEPT — latch set inside the lock (`test_lost_write_latch_is_set_before_the_future_is_forgotten`). |
| 1 | codex | P2 | Duplicate configured symbols opened two rows and kept one id. | ACCEPT — symbols de-duplicated in the open/close paths (`test_duplicate_config_symbols_open_one_row`). |
| 1 | codex | P3 | Session round-trip test name overstated ("survive reopen"). | ACCEPT — renamed. |
| 1 | codex | P3 | Extract the coverage state machine out of `Recorder`. | REJECT — refactor beyond this change. |
| 1 | cursor | — | NO P1/P2, no P3. | — |
| 2 | codex | P2 | A close that finds no row is dropped, so the outage is absent and gets certified. | REJECT — a gap row can only disappear through the `runs` CASCADE, which also deletes the session row; `advance_checkpoint` then raises and nothing is published. |
| 2 | codex | P2 | Liveness-only could never end on the same socket when acks landed late. | ACCEPT — one non-blocking `wait_ready(0)` per healthy probe; the degraded stretch is reported as a gap (`test_late_acks_clear_liveness_only_on_the_same_socket`). |
| 2 | codex | P2 | Coverage state survived `stop()` → `start()`. | ACCEPT — reset in `start()` (`test_restart_resets_coverage_state`). |
| 2 | codex | P3 | Flush-failure test covered one writer. | ACCEPT — parametrized over the four. |
| 2 | cursor | P3 | A previous run's future failing after a restart re-latches write-lost; barrier test asserts only the execution handler. | Accepted gaps. |
| 2 | cursor | P3 | Over-long docstring line. | ACCEPT — wrapped. |
| 3 | codex | — | NO P1/P2 FINDINGS. | — |
| 3 | cursor | P3 | `wait_ready` docstring forbids the event loop, but the collector calls `wait_ready(0)` there. | ACCEPT — docstring notes the zero-timeout case. |

## Final verification (B1c-1)

- `make test`: exit 0, merged coverage 92%.
- `make lint`: all checks passed.

## PR #286 claude-review (round 1, on 2c983af)

| Sev | Finding | Verdict |
|---|---|---|
| P1 | `on_healthy_probe` was awaited unbounded and did not race the stop event, so `stop()` could hang on a slow checkpoint. | ACCEPT — `_run_checkpoint`: bounded by `_HEALTHY_PROBE_TIMEOUT`, races `_ws_health_stop_event` (shared `_wait_unless_stopped`), skipped when not running; the recorder shields the writes it awaits. |
| P1 | A DB error opening the gap row skipped the reset on every probe, so a dead socket could stay un-reset (orders / positions / wallet have no REST backfill). | ACCEPT — at most 3 probes in a row; then the reset goes ahead. Not queued as an open row (nothing would close it): the gap is recorded at reconnect. |
| P2 | One lost write froze the checkpoint for the rest of the run. | ACCEPT — the lost write is recorded as a gap and the latch released. |
| P2 | `_try_gap_write` dropped any `ValueError`. | ACCEPT — `grid_db.RowNotFoundError`; other `ValueError`s are retried. |
| P3 | A retried fallback row stays `pending`. | Deferred (`tasks/todo.md`); it under-claims coverage. |
| P3 | Rule text for `wait_ready(0)`. | ACCEPT. |

## PR #286 claude-review (round 2, on 6b114cc) — APPROVED, no P0/P1

| Sev | Finding | Verdict |
|---|---|---|
| P2 | The lost-write gap started at "now − 75 s" when the callback ran, which can be later than the loss. | ACCEPT — the earliest unrecorded loss time is kept under the lock and used as the anchor. |
| P2 | The latch was released even when no gap could be recorded (stopping). | ACCEPT — it stays set in that case. |
| P2 | Synchronous DB writes on the loop every healthy probe. | Deferred (`tasks/todo.md`). |
| P3 | Public name for `_LIVENESS_MARGIN`. | Declined (as on #283). |
| P3 | "3 probes" wording is off by one. | ACCEPT — reset skipped on at most 2 probes. |
| P3 | `start()` did not reset `_liveness_only`. | ACCEPT. |

## Phase B1c-2 — local staged review (review-fix-loop-staged, 2026-09-30)

5 reviewers, 1 iteration, 0 CRITICAL in code → Ready to commit. The testing reviewer ran 18 mutations; one survivor was an untested guard (`_open_gap_start` was never cleared when an outage closed — now cleared and tested).

| Sev | Finding | Fix |
|---|---|---|
| WARNING | `PublicCollector.start()` wrapped only a timeout; any other `connect()` error escaped, left `_running=True` and the recorder emitted no sentinel (3 reviewers). | Any collector start failure is a `CollectorStartError`; `Recorder.start()` emits the sentinel for any exception, with the cause. |
| WARNING | The abandon check ran only right after `connect()`; a timeout during the readiness wait left a live, unowned socket. | Re-checked after the readiness wait (later replaced by `StartHandoff`, see below). |
| WARNING | `EventSaver.add_account()` registered the collector before starting it, so a failed start could not be retried; a skipped account stayed registered. | Registered only after a successful start; a skipped account is dropped. |
| WARNING | A 30 s launcher wait would kill a healthy but slow start (collectors up to ~21 s, then three REST calls at 10 s). | 60 s. |
| WARNING | Pre-existing, window widened: a backgrounded recorder ignores SIGINT until `run_until_shutdown`, so the launcher's kill did nothing during the start. | `recorder.main` installs the handlers before `start()`. |
| WARNING | Stale docs (runbook 15 s, 0039 rule file reference, sentinel paragraph, plan note contradiction) and 12 test gaps. | Fixed / tests added. |

Documented, not changed: an abandoned connect worker spins in pybit's retry loop until the endpoint answers; a WS row can precede the REST t=0 row and a fill during the snapshot is recorded twice; `PublicCollector.stop()` disconnects on the loop (pre-existing).

## Phase B1c-2 — external review (ext-code-review)

Engines: codex (`gpt-5.6-sol`, high) + cursor (`grok-4.7-high`). Rounds 4/4 (the limit). Every valid finding is fixed; the two from codex round 4 were fixed after the last round and have not been re-reviewed by an engine.

| Round | Engine | Sev | Finding | Verdict |
|---|---|---|---|---|
| 1 | codex, cursor | P1 | Cancelling `start()` (the new signal path) skipped the collectors' cleanup; `PublicCollector.stop()` then blocked on the lock the parked `connect()` holds. | ACCEPT — a cancelled start is handled like a timeout in both collectors. |
| 1 | codex | P2 | A cancelled start emitted no launcher sentinel. | ACCEPT. |
| 1 | codex | P2 | A `connect()` that raised part-way left its socket open. | ACCEPT — bounded disconnect. |
| 1 | codex | P3 | Stale rule line about lost writes; no cancellation / second-start tests. | ACCEPT. |
| 2 | codex | P2 | Writer init / run seeding failures emitted no sentinel. | ACCEPT — inside the sentinel guard. |
| 2 | codex, cursor | P2 | A cancel during the cleanup disconnect left `_running` / `_ws_client` set. | ACCEPT — state reset before the disconnect is awaited. |
| 2 | codex | P2 | Race between the worker's last abandon check and the owner giving up. | ACCEPT — `StartHandoff` (one lock): exactly one side closes the socket. |
| 2 | codex | P3 | Order test stubs the REST snapshot; launcher tests assert on script text. | Accepted gaps. |
| 3 | codex | P2 | A worker that raised after the owner gave up closed nothing. | ACCEPT. |
| 3 | codex | P2 | A signal landing as `start()` finished was lost. | ACCEPT — `Recorder.request_shutdown()`; handlers stay installed until `run_until_shutdown` replaces them. |
| 3 | cursor | — | NO P1/P2. P3: blank cause in the aborted-start log on cancellation; rule wording. | ACCEPT. P3 "EventSaver stays `_running` after a public start failure" — pre-existing, not changed. |
| 4 | codex | P2 | Inverse handoff race: a worker that raises just before the owner gives up is closed by neither. | ACCEPT — the worker calls `finish()` whether it returns or raises. |
| 4 | codex | P2 | An exception escaping the REST snapshot emitted no sentinel. | ACCEPT. |
| 4 | cursor | — | NO P1/P2. P3: a second signal during the emergency stop only requests shutdown (cannot interrupt a stuck synchronous public disconnect). | Accepted gap (pre-existing disconnect). |

## Final verification (B1c-2)

- `make test`: exit 0, merged coverage 92%.
- `make lint`: all checks passed.

## Phase B2a — local staged review (review-fix-loop-staged, 2026-10-01)

5 reviewers, 1 iteration, 0 CRITICAL → Ready to commit.

| Sev | Finding | Fix |
|---|---|---|
| WARNING | Rule/plan claim "the only snapshot `raw_json` reader is the wallet collateral path" missed live-check `shared_wallet.py` (also wallet-only). | Reworded: nothing reads position `raw_json`; wallet readers listed. |
| WARNING | No multi-symbol test mixing failure types. | Not added: `fetch_failed` resets per symbol and `synthetic` per side. |
| INFO | `leg_side` imported from the submodule while the writers come from the package. | Exported from `event_saver.writers`. |

## Phase B2a — external review (ext-code-review)

Engines: codex (`gpt-5.6-sol`) + cursor. Rounds 1/4.

| Round | Engine | Sev | Finding | Verdict |
|---|---|---|---|---|
| 1 | codex | P2 | Malformed handling starts after side indexing: a non-dict row aborts startup; a row with unusable `side`/`positionIdx` is ignored and labelled `absent_side`. | REJECT — the non-dict crash is pre-existing (`pos.get("symbol")` before B2a) and pybit returns dicts; by Bybit's contract `side` is `""` only for an empty position and hedge `positionIdx` is 1/2, so such a row is flat and `absent_side` ("known flat") stays true. |
| 1 | cursor | — | NO P1/P2; verified every per-side reader (replay seed, ground_truth, comparator, `get_latest_before`) with flat legs stored as `Buy`/`Sell` size 0. P3: `_rows` collapses duplicate sides; size/NULL telemetry asserted only on the malformed case; no `side=None` / missing `positionIdx` writer case. | `_rows` now asserts exactly one Buy + one Sell row. Others accepted gaps. |

## Final verification (B2a)

- `make test`: exit 0, merged coverage 92%.
- `make lint`: all checks passed.

## PR #288 claude-review (round 1, on 94f05e0) — APPROVED, no P0/P1

All six findings applied inline.

| Sev | Finding | Fix |
|---|---|---|
| P2 | A `get_positions` failure still produced `RECORDER_SNAPSHOT_OK`: its two placeholder rows counted as a position snapshot. | `_snapshot_positions` returns `(rows, fetch_failures)`; any failure → `RECORDER_SNAPSHOT_INCOMPLETE`, like a wallet failure. Test `test_position_fetch_failure_is_incomplete`. |
| P2 | No reader-level test that a flat-leg row supersedes the pre-close row in the replay seed. | `test_flat_leg_row_supersedes_pre_close_row` (`test_snapshot_loader.py`). |
| P2 | No comparator test with flat rows inside the live per-side stream. | `test_flat_rows_keep_stream_alignment` (`test_position_metrics.py`). |
| P2 | A row whose leg `leg_side` cannot resolve was dropped silently before the leg became `absent_side`. | WARNING with raw `side` / `positionIdx`. Test `test_unresolved_side_row_is_logged`. |
| P3 | No writer test for a missing `positionIdx` or `side=None`. | Two tests; both store `""`. |
| P3 | Rule said nothing reads position `raw_json`; `scripts/research_0045_im_mm_distribution.py` does (falls back to leverage 1). | Reworded to "no application code" and named the script. |

`make test` exit 0 (92%), `make lint` clean.

## PR #288 claude-review (round 2, on 5c7a5cc) — APPROVED, one P1 (pre-existing, out of scope)

| Sev | Finding | Verdict |
|---|---|---|
| P1 | Live `gridbot.position_fetcher` skips `side=""` rows, so a flat hedge leg's WS cache slot keeps the pre-close dict and the runner trades on it. | Valid (verified in code), pre-existing, live-bot. DEFERRED to a separate PR — not folded into a recorder PR; `tasks/todo.md` Follow-ups. |
| P2 | An unresolvable row was dropped and the legs became `absent_side` (known flat) with OK. | Fixed: an open unresolvable row → both synthesised legs `malformed`, symbol failed → INCOMPLETE. |
| P2 | A successful fetch with no row for the symbol (wrong scope) became `absent_side` + OK. | Fixed: `empty_response` marker + WARNING; not INCOMPLETE (never-traded symbols); B2c decides. |
| P2 | `PositionWriter` `Decimal(str(pos.get("size", "0")))` drops a row with `""` numerics (pitfall 14). | Fixed: `or "0"` for size / entryPrice; test with `size=""`, `entryPrice=""`, `positionIdx=2`. |
| P3 | Comparator alignment test used full telemetry on flat rows. | Fixed: NULL telemetry; asserts `position_pairs_missing_telemetry == 1`. |
| P3 | `get_latest_before` tie on `exchange_ts` undefined. | Fixed: `id` tiebreak + test. |
| P3 | `_write_initial_rest_snapshot` docstring missed the new INCOMPLETE condition. | Fixed. |
| P3 | One-way flat row logged a WARNING on every start. | Fixed: flat unresolvable row → INFO. |

`make test` exit 0 (92%), `make lint` clean.

## PR #288 claude-review (round 3, on aee9c25) — APPROVED, no P0/P1

| Sev | Finding | Verdict |
|---|---|---|
| P2 | `PositionWriter` `int(pos.get("updatedTime", 0))` drops a row with `updatedTime=""` (and a missing key gives the 1970 epoch). | Fixed: missing / empty / 0 → `local_ts`. Test with `updatedTime=""`, `size=""`, `positionIdx=2`. |
| P2 | No live-check test that a flat-leg row drops the closed leg from `net_unrealised_per_pair`. | Added `test_flat_leg_row_drops_the_closed_leg`. |
| P2 | Gridbot hazard tracked only in `tasks/todo.md`; open a GitHub issue + rule entry. | Rule entry added under `gridbot.md` Key Pitfalls. Issue NOT opened (outside this PR's scope) — left to the user. |
| P3 | `_is_flat_position_row` caught `Exception`. | Narrowed to `(InvalidOperation, ValueError, TypeError)`. |
| P3 | Sentinel docstring: position_count==0 is no longer the scope signal; `empty_response` stays OK. | Docstring updated. |
| P3 | `ORDER BY exchange_ts DESC, id DESC` may add a temp sort. | REJECT — `EXPLAIN QUERY PLAN` (in-memory schema) shows the same index search for both orderings, no temp B-tree: `id` is the rowid alias, implicitly last in every SQLite index. |

`make test` exit 0 (92%), `make lint` clean.

## PR #288 claude-review (round 4, on b3bf517) — APPROVED, no P0/P1

| Sev | Finding | Verdict |
|---|---|---|
| P2 | A real row that fails conversion is `malformed` but the snapshot stays OK. | Fixed: the symbol fails → INCOMPLETE (counted once with an unresolved open row); test extended. |
| P2 | All configured symbols `empty_response` (mis-scoped key) stays OK. | DEFERRED — design input for B2c, which owns `empty_response`; `tasks/todo.md`. |
| P2 | Comparator `_pair_side` takes the first unclaimed live row in the window, so an extra live row (now incl. flat rows) can steal a backtest row's match. | DEFERRED — pre-existing for any extra live row (Bybit pushes on order create/amend/cancel); comparator pairing change belongs in its own PR; `tasks/todo.md`. |
| P2 | Asymmetric-stream comparator test. | DEFERRED with the item above. |
| P2 | Open a GitHub issue for the gridbot `side=""` hazard. | Not done — outside this PR; left to the user (rule + todo entries exist). |
| P3 | Empty `updatedTime` could try frame `creationTime` before `local_ts`. | DEFERRED — V5 position frames carry `updatedTime`; `tasks/todo.md`. |

`make test` exit 0 (92%), `make lint` clean.

## Phase B2b — local staged review (review-fix-loop-staged, 2026-10-02)

5 reviewers, 1 iteration → Ready to commit.

| Sev | Finding | Verdict |
|---|---|---|
| CRITICAL (claimed) | Seed anchors should use `exchange_ts`, not `local_ts`. | REJECT — coverage is measured on the recorder clock; a push is a full snapshot as of receipt, and a quiet leg's push keeps a Bybit `updatedTime` that can predate the recorder start, which would SKIP permanently. Pinned by `test_seed_anchor_uses_receipt_time_not_exchange_time` + code comment. |
| WARNING | Plan note cited a stale line number for the probe constant. | Fixed. |

Security, performance, testing: no issues (all coverage queries index-backed).

## Phase B2b — external review (ext-code-review)

Engines: codex (`gpt-5.6-sol`) + cursor. Rounds 2/4 → SUCCESS.

| Round | Engine | Sev | Finding | Verdict |
|---|---|---|---|---|
| 1 | codex | P2 | Session check ran before gaps; the recorder freezes the checkpoint while a gap is open, so a real open gap reported a generic "not covered" without bounds/status. | ACCEPT — gaps checked first; test `test_open_gap_reported_even_when_checkpoint_froze`. |
| 1 | codex | P3 | Table presence inspected per call, not once per DB open. | Accepted gap (two cheap `sqlite_master` reads). |
| 1 | codex | P3 | No aware-UTC test. | ACCEPT — `test_aware_utc_rows_compare_as_naive_utc`. |
| 1 | cursor | — | NO P1/P2. P3: no back-to-back-sessions test; refused wallet row still extends the interval; reason quotes newest session. | Test added; the other two accepted (safe-direction SKIP / cosmetic). |
| 2 | codex, cursor | — | NO P1/P2. P3: wallet `get_latest_before` lacks an `id` tie-break. | ACCEPT — tie-break + repository test. |
| 2 | cursor | P3 | A lag of 86–~95 s can still SKIP when the checkpoint flush is slow (barrier taken before flush). | Accepted gap — plan formula; default 2 m clears it; failure mode is SKIP, not a false PASS. |

## Final verification (B2b)

- `make test`: exit 0, merged coverage 92%.
- `make lint`: all checks passed.

## PR #289 claude-review (round 1, on fe0ba94) — APPROVED, one P1 (declined)

| Sev | Finding | Verdict |
|---|---|---|
| P1 | Coverage interval should also reach back to active-order seed rows (`OrderRepository.get_active_at`). | DECLINED — the plan's Known limitations put replay's active-order seed completeness out of scope (#274); anchoring on far grid levels would stretch every interval toward run start. Recorded in `live-check.md` + plan notes. |
| P2 | Gate expression duplicated in three entry points. | Fixed: one `main._gate_skip_reason` helper. |
| P2 | Lag floor tested only for `run_single`. | Fixed: `run_shared_single`, `run_watch` and `main()` → `EXIT_FAIL` tests. |
| P2 | Permanent-SKIP consequence of seed anchoring undocumented. | Fixed: rule + plan note (recover with a recorder restart). |
| P3 | `"USDT"` wallet coin coupling to replay's `SeedConfig.wallet_coin`. | Comment added. |
| P3 | `--lag` help silent on the floor. | Help text updated. |
| P3 | `window.py` imports `event_saver.collectors` at module level. | Accepted gap (live-check already depends on gridbot/event_saver). |

`make test` exit 0 (92%), `make lint` clean.

## PR #289 claude-review (round 2, on 0b935d0) — APPROVED, P1 = observability

| Sev | Finding | Verdict |
|---|---|---|
| P1 | A gap only in the seed stretch silently SKIPs every later window of the run. | Fixed: when every overlapping gap ended before `window.start`, the reason says it only touches the seed rows and to restart the recorder (no per-tick log line — the SKIP line already prints each tick). Test added. |
| P2 | Move the constants to a dependency-free `event_saver/constants.py` to keep pybit out of live-check's import graph. | DECLINED — `event_saver/__init__.py` imports `main`, the reconciler, collectors and writers, so any `event_saver.*` import loads the package anyway; a real fix means moving the constants out of event_saver. The import has no side effects. |
| P2 | `--shared` printed "no data in window" before the coverage reason. | Fixed: gate reasons first in all modes; test added. |
| P2 | CLI-level tests only exercised the missing-session path. | Fixed: gap-path tests for `--once`, `--shared`, `--watch`. |
| P2 | `(+N more)` branch untested. | Test added. |
| P3 | Gate assumes the strat symbol is recorded (gap rows per configured symbol). | Comment added. |
| P3 | `lag` config/YAML silent on the floor. | Field description + YAML comment. |

`make test` exit 0 (92%), `make lint` clean.

## PR #289 claude-review (round 3, on 82f04ac) — APPROVED, P1 = startup race

| Sev | Finding | Verdict |
|---|---|---|
| P1 | The recorder stamps `connected_at` after the subscription acks; a push in that window has `local_ts` before the session and, as a seed row, SKIPs every window with no gap to explain it. | Fixed live-check side (no recorder redeploy): seed anchors are clamped up to the run's first `connected_at` — within a `run_id` a pre-session row can only come from that race. A window that itself starts before the session still SKIPs. `test_seed_row_before_session_skips` replaced by the race test + `test_window_before_session_still_skips`. |
| P2 | Wallet coin hardcoded. | Fixed: taken from replay's `SeedConfig.wallet_coin` default. |
| P2 | Move the constants to `grid_db` and drop the `event-saver` dependency. | DECLINED — live-check already loads pybit through replay/backtest (`import replay.engine, backtest.runner` → `pybit` in `sys.modules`), so the move buys nothing. |
| P2 | No direct repository tests. | Added `TestPrivateStreamCoverageReads` (bounds, open gap, scope, ordering; sessions oldest-first + account scope). |
| P2 | Check the strat symbol against the recorder's private symbol list. | DECLINED — a symbol not recorded privately has no executions, so it already SKIPs on the empty window; no false PASS. |
| P2 | `_gate_skip_reason` unannotated. | Annotated + Google docstring. |
| P3 | Count printed after the restart hint. | Swapped; test added. |
| P3 | Memoise the table check. | Accepted gap (two cheap reads). |

`make test` exit 0 (92%), `make lint` clean.

## PR #289 claude-review (round 4, on 7b89efd) — APPROVED, P1 = availability (documented + follow-up)

| Sev | Finding | Verdict |
|---|---|---|
| P1 | A flat hedge leg pins the seed anchor at the run-start REST row, so one WS blip SKIPs every later window. | Confirmed and broader: `updatedTime` moves only on a size change, so ANY leg with no fill since recorder start keeps the startup row (exchange_ts = snapshot_ts) as its seed row; SKIP lasts until that leg fills (flat leg with no orders: until restart). Fail-safe (SKIP, not PASS). Taken as the reviewer's option (b): plan Known limitations + rule + SKIP-reason wording; remedy (recorder REST resnapshot on gap close) recorded in `tasks/todo.md` Follow-ups — a recorder change + deploy. |
| P2 | Config `lag` below the floor not rejected at load. | Fixed: `field_validator("lag")` → `parse_lag`; test added. |
| P2 | `--shared` ran exec-count / unknown-PnL aggregates before the gate. | Fixed: gate first, aggregates only after it passes. |
| P2 | `--shared` seeds from `MultiSeedConfig.wallet_coin`; `load_wallet_curve` hardcodes "USDT". | Comment notes the separate field (same default). The `load_wallet_curve` hardcode predates this PR — not changed. |
| P2 | No test for a later session alone covering. | Test added. |
| P3 | Hint wording singular. | Reworded ("gap(s) only touch"). |
| P3 | Memoise the table check. | Accepted gap (third request; two cheap reads). |
| P3 | Rule bullet too long. | Split into sub-bullets. |

`make test` exit 0 (92%), `make lint` clean.

## Phase B2c — plan review (codex `gpt-6-astra`, medium, 2026-10-03)

No P1. Accepted: anchor time from Bybit `updatedTime` in `raw_json` (a REST row's `exchange_ts` is the local fetch time); end anchors looked up once per check; extra tests (end-anchor-only gap, clamping, executions outside the window, late order row, latest-order ordering, market close, same-millisecond fills, clock skew). Documented, not fixed: missing `reduceOnly` coerced to `False` by the normalizer shared with the live gridbot; market fills have no order row; same-millisecond fills rely on the second push. D1–D4 agreed (D2 with anchor reuse; codex measured ≈0.21 s per million-row leg).

## Phase B2c — local staged review (review-fix-loop-staged)

5 reviewers, 1 iteration → Ready to commit. Four findings tagged CRITICAL, none confirmed:

| Claim | Verdict |
|---|---|
| Executions not filtered by `account_id`. | Downgraded — a recorder run is one account; `sum_realized` / `live_exec_count` use the same run + symbol scope; worst case a false SKIP. |
| No `(run_id, symbol, exchange_ts)` index on `private_executions`. | Downgraded to INFO — would need a migration; the window sums already query this way. |
| `_anchor_update_time` fallback untested; new repository methods untested. | Tests added (4 fallback cases; `TestEndAnchorReads`). |
| "All modes" RED test missing. | Covered by the per-mode SKIP tests. |

## Phase B2c — external review (ext-code-review)

Engines: codex (`gpt-5.6-sol`) — round 1: NO P1/P2, no P3 (213 focused tests passed). Cursor: dropped for this run after two failures (network "Connection stalled repeatedly", then no output at the 600 s limit). Result: SUCCESS on codex alone.

## Final verification (B2c)

- `make test`: exit 0, merged coverage 92%.
- `make lint`: all checks passed.
- Mutation checks: 4/4 caught (fill boundary, Bybit-clock anchor time, leg attribution, `empty_response` unfit).

## PR #290 claude-review (round 1, on the B2c commit) — APPROVED, no P0/P1

| Sev | Finding | Verdict |
|---|---|---|
| P2 | Executions query lacks `account_id` (tenant rule in `grid-db.md`; `_execution_legs` already keys by account). | Fixed — supersedes the local-review downgrade: `end_anchor_skip_reason(..., account_id, ...)`, executions filtered by account, one order lookup; test that another account's fill is ignored. The audit-port test now seeds its execution on the run's account. |
| P2 | `IN (...)` over an unbounded id list (old SQLite 999-variable cap). | Fixed: `get_latest_by_order_ids` chunks by 500; 1200-id test. |
| P2 | `net_unrealised_per_pair` re-runs the end-anchor lookup instead of reusing the gate's rows. | DEFERRED — the gate and `collect` sit on either side of a multi-second replay in separate sessions; threading ORM rows across needs a `main` restructure. The race needs a recorder flush later than the 2 m lag. |
| P3 | `get_latest_received_before` run/account scope untested. | Foreign-account row added to the repository test. |

`make test` exit 0 (92%), `make lint` clean.

## Phase B3 — local staged review (review-fix-loop-staged, 2026-10-03)

5 reviewers, 1 iteration → Ready to commit.

| Claim | Verdict |
|---|---|
| CRITICAL: `_post_gap_snapshot_again` written from the WS thread (data race). | REJECT — every gap path is an event-loop callback (collector `_handle_reconnect` inside `_ws_health_check_once`; lost-write gap via `call_soon_threadsafe`). But the review exposed a REAL lost-rerun window: the `run_coroutine_threadsafe` future is marked done by a later loop callback, so a gap handled after the coroutine returned saw "in flight", set the flag and was never rerun. Fixed: `_post_gap_snapshot_running`, owned by the coroutine (cleared in its `finally`); RED test `test_gap_right_after_the_last_run_is_not_lost`. |
| CRITICAL: `BybitRestClient` constructed on the loop. | Downgraded — pybit `HTTP()` init does no network I/O; the startup snapshot does the same. |
| Missing tests: REST client failure; one-of-many symbols failing. | Client-failure test added; multi-symbol not added (per-symbol `continue`). |

Security, docs: no issues.

## Phase B3 — external review (ext-code-review)

Codex (`gpt-5.6-sol`), round 1: NO P1/P2; P3 (stop duration unbounded in the test) → `stop()` now asserted < 2 s with REST blocked. Cursor: no output at the 600 s limit (its third failure this session) — not restarted. Result: SUCCESS on codex.

## Final verification (B3)

- `make test`: exit 0, merged coverage 92%.
- `make lint`: all checks passed.
- Mutation checks: 3/3 caught (placeholders after a gap, no coalescing, no rerun).

## PR #291 claude-review (round 1) — CHANGES REQUESTED, one P1

| Sev | Finding | Verdict |
|---|---|---|
| P1 | Post-gap seeds un-SKIP windows whose active-order seed is still stale (orders not backfilled; before B3 the stale position seed incidentally covered it). | Fixed: `ground_truth._order_resting_across_gap` SKIPs a window whose seeded active order last updated before an earlier gap ended; coverage docstring updated; tests (order resting across the gap → SKIP, orders after the gap → pass). |
| P2 | Wallet written even when positions did not land (inconsistent seed pair). | Fixed: positions first, wallet only when rows were written and no symbol failed. |
| P2 | Degenerate wallet reading (empty USDT `walletBalance` / `totalEquity` → 0) becomes the newest seed. | Fixed: wallet `evidence_only` guard; 3 parametrized tests. |
| P2 | An empty post-gap snapshot logged at INFO. | Fixed: WARNING `wrote nothing`. |
| P2 | No test that a public gap does not re-snapshot. | Added. |
| P3 | Timing-dependent tests; patchers started inside the factory. | Fixed: event-driven waits; patchers started before `yield` with `try/finally`. |
| P3 | Duplicate synthetic-marker sets; per-snapshot REST client. | Deferred to `tasks/todo.md`. |

`make test` exit 0 (92%), `make lint` clean.

## PR #291 claude-review (round 2) — APPROVED, one P1

| Sev | Finding | Verdict |
|---|---|---|
| P1 | Post-gap wallet guard omits `totalAvailableBalance`; an empty one is stored as 0, becomes the newest seed and `load_wallet_seed_full` refuses it. | Fixed: added to the `proven` predicate (guard moved above the `availableToWithdraw` comment it had split); parametrized case added. |
| P2 | `_order_resting_across_gap` reason has no remedy; the SKIP can last long. | Fixed: reason says later windows SKIP until the order is updated/replaced or the recorder restarts; #274. |
| P2 | Orders created during a gap are invisible to the order check. | Documented in Known limitations + `live-check.md` (#274; symptom is FAIL, not PASS). |
| P2 | Multi-symbol veto (`position_count and not failed_symbols`) untested. | Added: second symbol fails → first symbol's rows written, no wallet row. |
| P3 | Order check compared `exchange_ts` with recorder-clock gap bounds. | Fixed: seed set by `exchange_ts`, gap comparison on `local_ts`. |
| P3 | Pin recorder constants to live-check/replay; long-lived REST client. | Deferred (already in `tasks/todo.md`). |

## PR #291 claude-review (round 3) — APPROVED, no P0/P1

| Sev | Finding | Verdict |
|---|---|---|
| P2 | An `empty_response` symbol was skipped but not counted as failed, so a multi-symbol post-gap snapshot still wrote the wallet. | Fixed: under `evidence_only` every skipped symbol counts as failed; test (second symbol empty → no wallet row). |
| P2 | `proven` was set before the USDT row was built; a USDT row that failed conversion left a non-USDT-only batch "proven". | Fixed: set after the append; test (malformed USDT + good SOL → nothing written). |
| P2 | `_order_resting_across_gap` scoping untested. | Added: other symbol / account / run → no SKIP. |
| P3 | Unreachable open-gap branch. | Kept, commented as defensive. |
| P3 | Pin `_WALLET_SEED_COIN` / `_UNPROVEN_SYNTHETIC`. | Added `tests/integration/test_recorder_seed_contract.py`; follow-up removed. |

## PR #291 claude-review (round 4) — APPROVED, one P1

| Sev | Finding | Verdict |
|---|---|---|
| P1 | Wallet guard checked only empty, not zero; replay refuses `totalEquity` / `totalAvailableBalance` <= 0. | Fixed: positive values required (reusing the parsed Decimals); two zero cases added. A zero USDT `walletBalance` is NOT refused by replay (collateral can back the account), so it stays allowed. |
| P2 | `run_coroutine_threadsafe` on a closed loop latches the running flag. | Rejected: every caller is a callback on that same running loop (the bot's own summary confirms it), so the loop cannot be closed there. |
| P2 | `_order_resting_across_gap` SKIPs for days on a long-resting grid order. | Deferred: B3 decision 1 is positions + wallet only; spelled out in Known limitations; post-gap `_snapshot_open_orders` follow-up in `tasks/todo.md`. |
| P3 | Long-lived REST client. | Deferred (already in follow-ups). |
| P3 | Guard `_write_post_gap_snapshot` against no account / run. | Rejected: the single caller already checks both (no impossible-case handling). |
| P3 | Missing session should be reported before the order SKIP. | Fixed: order check moved after the no-session return. |
