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

### Final verification

- `make test`: exit 0, merged coverage 91% (gridcore 94.8%).
- `make lint`: all checks passed.
- Shipped on PR #281 (feature/0110-rest-exec-recovery); merge awaits explicit user approval.
