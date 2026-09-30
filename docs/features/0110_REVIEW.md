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
