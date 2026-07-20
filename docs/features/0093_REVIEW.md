# Feature 0093 — apps/importer: review trail

Branch `feature/0093-importer`. Plan: `docs/features/0093_PLAN.md`.

## Internal review (/review-fix-loop-staged)

5 parallel category reviewers (quality/security/performance/testing/docs),
1 iteration, **0 criticals**. Applied: 2 missing type hints, 2 docstring
tightenings, validate.py unit tests (34% → 74%: `_coerce_kline_ts`,
`check_ohlc` end-to-end vs synthetic klines + error paths, probe skip
paths). Rejected with evidence: "O(n²) overlap loop" (two-pointer merge,
O(n+m)); "batch[-1] IndexError" (transports never yield empty batches);
path-traversal guards on operator CLI args (path choice is the feature;
no speculative validation per project constraints); engine/session
dispose (matches recorder/pnl_checker process-lifetime pattern).

## External review trail (/ext-code-review)

Engines: codex (`codex exec --sandbox read-only`) + cursor
(`agent --mode ask`), both per round. Rounds: 4. Criteria:
`commands/code_review.md`.

### Findings raised → verdicts

Round 1 (cursor 6 P2 + 5 P3; codex 1 P2 + 3 P3, overlap deduped):
- ACCEPT+fixed: klines fetch pushed day window into SQL (datetime cols;
  Python filter stays as epoch fallback); post-stale-reclaim O_EXCL race
  → `ImportLockHeldError`; kline HTTP params aligned on shared Z-suffix
  `iso_utc`; empty-string `next_cursor` terminates; window bounds bind
  aware-UTC (naive binds vs Postgres `timestamptz` shift with SESSION
  timezone); `--batch-size` rejects ≤ 0. P3s fixed as trivially cheap:
  mocked-subprocess `smoke_replay` contract tests; http-no-bounds e2e test.
- REJECT: density last-day `covered_seconds` "bug" — coverage of the last
  day of a continuous window genuinely starts at midnight; tz-aware sqlite
  fixture — sqlite bind formatter drops tzinfo, cannot round-trip aware
  datetimes (conversion unit-tested at function level; documented in
  test_fetch_db.py).
- Accepted gaps (P3): response-shape guards for camelCase/nested payloads
  (contract documented; unknown shapes fail closed); interval spellings
  beyond ("1","1m"); validate.py module size.

Round 2 (codex NO P1/P2 + 1 P3; cursor 2 P2 + 4 P3):
- ACCEPT+fixed: OHLC sample day falls back to nearest tick-bearing day
  when the midpoint day is empty (collector outage); empty-rows page with
  truthy cursor terminates (progress guarantee); importer added to root
  dev-group so bare `uv run python -m importer.main` works; tz helpers
  promoted to public `aware_utc`/`iso_utc`.
- Accepted gaps (P3): single long read session on a Postgres source
  (resume heals a timeout abort); kline-HTTP retry/column-alias asymmetry
  (kline endpoint explicitly out of client scope beyond symbol/start/end).

Round 3 (codex 1 P2; cursor NO P1/P2 + 4 P3):
- ACCEPT+fixed: `smoke_replay` now requires a parsable `Net PnL` metric
  (missing/unparsable fails) + regression tests; `--ohlc-threshold`
  validated to (0, 1].
- Accepted gaps (P3): `run_validation` runs smoke even after OHLC failure
  (deliberate — full diagnostics per run); no HTTP happy-path e2e through
  main (transport fully unit-tested).

Round 4 — confirmation (codex 1 P2-labeled nit; cursor NO P1/P2 + 3 P3):
- ACCEPT+fixed: `math.isfinite` instead of `isnan` (inf rejected) + test;
  kline HTTP `.get("rows") or []` (null-rows symmetry with ticker path).
- Accepted gap (P3): multi-day batch emits one progress log per batch
  boundary, not per day crossed (cosmetic).

### Final status

86 importer tests green; `make lint` clean; full `make test` exit 0,
TOTAL coverage 91% (gate ≥ 88), importer package ~91%.

```
ext-code-review trace
  scope: 26 files
  engines: both (codex + cursor)
  iterations: 4/10
  findings: raised 31, accepted 14 (fixed 14), rejected 4, P3-ignored 13
  verification: make test exit 0 (TOTAL 91%), lint clean
  result: SUCCESS
```
