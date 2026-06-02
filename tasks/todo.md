# Feature 0064 — 110017 retry-storm: dirty-mirror REST refresh + circuit-breaker

Plan: docs/features/0064_PLAN.md  |  Branch: feature/0064-truncate-breaker-dirty-refresh

## Phase 1 — Data layer
- [x] `bybit_adapter/error_codes.py`: `ORDER_QTY_TRUNCATED_TO_ZERO = 110017` (+ re-export in `__init__`)
- [x] `executor.py`: `is_truncate_error()` classifier (module-level fn; reuse `_ERR_CODE_RE`)
- [x] `config.py` StrategyConfig: `dirty_refresh_enabled`, `dirty_rest_refresh_min_interval_seconds`, `truncate_breaker_{max_consecutive,window_seconds,cooldown_seconds,reconcile}`

## Phase 2/3 — TruncateBreaker
- [x] `truncate_breaker.py`: scope-key sliding window, trip/cooldown, in-cooldown no-op, reset-on-success

## Phase 2 — runner dirty-mirror refresh
- [x] ctor: `rest_client`, `truncate_breaker`, `on_truncate_breaker_tripped`, `clock`
- [x] state: `_position_dirty`, `_last_dirty_rest_at`, `_last_rest_position_size`, `_truncate_breaker_reconcile_count` (+ property)
- [x] `_refresh_position_size_from_rest(direction, *, force=False)`
- [x] `_execute_place_intent` pipeline: is_blocked → dirty-refresh → guard → submit → breaker bookkeeping
- [x] exclude 110017 from retry queue; drop wire-id reuse on 110017
- [x] `on_position_update`: dirty WS gate + WS-match clear

## Phase 3 — orchestrator wiring
- [x] pass `rest_client`; wire `on_truncate_breaker_tripped` → `_force_reconcile_strat`
- [x] `_force_reconcile_strat` (orders reconcile + force position refresh, rate-limited)
- [x] `_health_check_once`: DEBUG log trip counts

## Verification
- [x] All new tests pass (TDD: red → green) — 40 new tests
- [x] Full repo suite green — 2382 passed, 3 skipped
- [x] Adversarial review pass — 2 confirmed (P3 positionIdx hardening, P2 force=True test), 1 rejected; both addressed
- [x] Update RULES.md — added "110017 retry-storm self-heal + circuit-breaker" section
- [x] docs/features/0064_REVIEW.md findings — F1 (dirty-clear close-only), F2 (throttle on every attempt), F3 (`_clear_dirty` episode-scoped reset), F4 (dead test cond), F6 (negative tests) applied; F5 declined (plan-sanctioned, optional).
- [x] Review v3 findings — P2 (split forced-reconcile try blocks so position resync survives an order-reconcile failure) applied + test; P3.1 (`rest_client=None` backstop test) + P3.2 (short-side refresh test) added; P3.3 non-issue. Full suite 2393 passed.
- [x] PR #157 bot review (await-review) — APPROVED, no P0/P1; user requested fix of P2 #1 + #2:
  - #1 `dirty_rest_refresh_failure_count` metric (REST refresh failures) + health-sweep surfacing
  - #2 `_dirty_ws_mismatch_streak` + threshold WARNING (WS feed stuck) — reset via `_clear_dirty`
  - +8 tests; full suite 2401 passed. (bot #3 already covered; #4/#5 not requested.)
- [x] PR #157 bot review round 2 — APPROVED, no P0/P1; user requested #1-#4 (cosmetic):
  - #1/#2 exception `type(e).__name__` in dirty-refresh WARNINGs; #3 Bybit error-code doc link in `error_codes.py`; #4 inline comment on breaker scope key. (#5 already covered.)
  - No behavior change; full suite 2401 passed.

## What could break (revisit after impl)
- Mock(spec=IntentExecutor) auto-creating truthy methods → classifier kept module-level
- DirectionType is StrEnum ('long'/'short'); dict keys interop with intent.direction str
- position_ratio/liq calc reads WS state, not gated mirror (intentional, out of scope)
