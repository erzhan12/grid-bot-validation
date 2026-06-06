# 0067 Code Review — Suppress LowBalanceSkip Log Spam

**Date:** 2026-06-06  
**Plan:** `docs/features/0067_PLAN.md`  
**Scope reviewed:** `apps/gridbot/src/gridbot/config.py`, `apps/gridbot/src/gridbot/runner.py`, `apps/gridbot/tests/test_runner_lowbalance_storm.py`, `RULES.md`

## Findings

No blocking findings.

### P3 — `RULES.md` test count is stale

`RULES.md` says the feature added 16 `test_low_balance_skip_*` tests, but the
current implementation has 17 test functions in the 0067 section after adding
`test_low_balance_skip_summary_emits_via_drain_hook`. This is documentation-only
drift; the behavior and test coverage are fine.

Recommended fix: update the count in `RULES.md` from 16 to 17, or avoid a hard
count there.

### P3 — `runner.py` continues to absorb feature-specific state machines

Feature 0067 adds roughly 200 lines of state, reconciliation, summary, and tests
around an already large `runner.py`. The logic is currently understandable and
well colocated with `_preflight_blocks_open`, but the module is becoming a
collection point for several independent operational state machines.

This is not a blocker for this logging feature. It is a refactor pressure point:
future additions of this shape should consider a small helper object for
stateful log aggregation to keep `StrategyRunner` focused on dispatch behavior.

## Fixed Since Previous Review

- The summary-hook coverage gap is fixed by
  `test_low_balance_skip_summary_emits_via_drain_hook`, which verifies summary
  emission through `_drain_pending_chase_intents()`.
- The startup-summary concern is covered in the same test with a high fake
  monotonic baseline. The production dispatch order calls
  `_drain_pending_chase_intents()` before generating/executing a tick's intents,
  so an empty first drain advances `_skip_summary_last_emit` before the first
  skip can be recorded.
- The new E731 lambda assignments in the intra-tick flutter test were removed.

## Plan Compliance

- The preflight accept/reject decision is unchanged.
- Fail-open remains evidence-neutral: no skip state, no scratch, no summary
  window increment.
- Genuine skips increment the summary window independently of transition logging.
- Per-intent DEBUG is suppressed when transition logging is enabled and restored
  when it is disabled.
- ENTER/EXIT edges resolve at sample boundary, not inline per intent.
- `_skip_tick_seen` is keyed by `(direction, side)` and uses sticky blocked
  semantics within a sample.
- Scratch clear is unconditional, including runtime flag flips.
- Idle-timeout sweep covers active keys outside the scratch set.
- Summary and edge reconcile are placed before the chase-buffer early return.
- The `Position update` INFO heartbeat is untouched.
- Config fields are per-strategy, default-on, and kill-switchable.

## Test Review

The added tests are broad and targeted. They cover transition-only logging,
recovery EXIT, no intra-tick flutter, fail-open neutrality, interleaved dispatch
events, sibling fail-open preservation, per-key independence, idle timeout,
runtime flag flips, summary flag combinations, empty summary windows, summary
emission via the production drain hook, and both kill-switches off.

The tests use a fake monotonic clock for interval/idle determinism, `caplog` for
log assertions, and direct preflight/reconcile calls for branch-level behavior
plus real dispatch handlers where production ordering matters.

## Verification

```bash
uv run pytest -q apps/gridbot/tests/test_runner_lowbalance_storm.py
# 66 passed in 0.15s
```

```bash
uv run ruff check apps/gridbot/src/gridbot/config.py apps/gridbot/src/gridbot/runner.py apps/gridbot/tests/test_runner_lowbalance_storm.py
# failed: remaining reported issues are pre-existing E402 import-layout findings
# in runner.py and pre-existing E731 provider lambdas above the added 0067 test
# section. The new 0067 intra-tick lambda assignments from the prior review are
# fixed.
```
