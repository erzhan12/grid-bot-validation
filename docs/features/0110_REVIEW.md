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
