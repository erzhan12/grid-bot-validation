# 0069 — Auto state-divergence detector + on-demand forced reconcile (issue #151)

Plan: docs/features/0069_PLAN.md  |  Branch: feature/0069-divergence-detector

## Status: implementation + tests + docs COMPLETE (adversarial review running)

- [x] error_codes.py — `ORDER_LINK_ID_DUPLICATE = 110072`
- [x] executor.py — `is_network_error`, `is_duplicate_link_error` classifiers
- [x] config.py — 7 new `divergence_*` StrategyConfig fields (ge=1 / gt=0 constraints)
- [x] runner.py — `_record_placement_failure` (signal 1), `clear_dedup_cache`,
      `rest_position_size` (pure read), `on_divergence_failure_mix` ctor param,
      `_placement_failure_window`, two `_execute_place_intent` call sites
- [x] orchestrator.py — `_force_reconcile_strat` refactor (`direction:str|None`,
      `emit_breaker_warning`, `-> bool`, both-directions internal), wrapper
      `_trigger_divergence_reconcile`, `_enqueue_post_recovery_reconcile`,
      `_divergence_size_check_once`/`_interval`, signal-1 wiring, signal-2 edge in
      `_health_check_once`, signal-3 gate+priming, signal-4 enqueues (3 private
      sources) + pinned `_tick` drain + order-sync fast-track, new __init__ state
- [x] analyze.py — merged `force_reconcile_fired` event_coverage key + label + scan
- [x] Tests: test_runner_divergence.py, test_orchestrator_divergence.py,
      test_executor.py classifiers, test_config.py, test_analyze.py
- [x] Docs: RULES.md 0069 section, SKILL.md Table 13 note
- [x] Full suite green: 1258 passed, 1 skipped (apps/gridbot + packages)

## Not committed — awaiting user review/approval before any commit.
