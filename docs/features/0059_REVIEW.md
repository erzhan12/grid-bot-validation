# Feature 0059 Code Review

**Plan:** `docs/features/0059_PLAN.md`  
**Scope:** Schema, writers, backtest emit, comparator aggregates, reporter output, migration script, and unit tests  
**Review pass:** 2 (post-fix)

---

## Verdict

**Approve.** The implementation matches the plan across all five phases. Pass-1 findings are resolved; no blocking or non-blocking defects remain.

---

## Resolved From Prior Review

| Pass-1 ID | Resolution |
|---|---|
| P3 — `fold_metrics_into` docstring said "21 **new**" fields | Fixed — now reads *"21 telemetry aggregate fields"* with *"21 total = 12 pre-existing + 9 from 0059"* (`position_metrics.py:268-270`). |
| P3 — `load_position_snapshots` Raises docstring stale | Fixed — module docstring and `Raises:` block now list 0034 / 0056 / 0059 probes (`position_loader.py:8-11, 123-126`). |
| P3 — No fold test for `cur_realised_usdt_*` / `cum_realised_usdt_*` | Fixed — `test_cur_cum_realised_usdt_per_snapshot_aggregates` asserts mean/max for both fields (`test_position_metrics.py:326-349`). |

---

## Plan Implementation Check

| Plan item | Status | Evidence |
|---|---|---|
| **Phase 1** — ORM `position_value` column on `PositionSnapshot` | Pass | `shared/db/src/grid_db/models.py:398-399` |
| **Phase 1** — Idempotent migration script | Pass | `scripts/migrate_0059_position_value.py` |
| **Phase 1** — `bulk_insert` column dict includes `position_value` | Pass | `shared/db/src/grid_db/repositories.py:980` |
| **Phase 1** — Repository round-trip tests (NULL + non-NULL) | Pass | `shared/db/tests/test_repositories.py:1587-1618` |
| **Phase 2** — Live WS writer reads Bybit `positionValue` | Pass | `apps/event_saver/.../position_writer.py:255-260` |
| **Phase 2** — Recorder REST snapshot + zero-row NULL | Pass | `apps/recorder/src/recorder/recorder.py:531-536, 566` |
| **Phase 2** — Backtest emit: tracker state when in position, explicit `Decimal("0")` when flat | Pass | `apps/backtest/src/backtest/runner.py:675-687, 704` |
| **Phase 3** — Loader probe for missing `position_value` column | Pass | `apps/comparator/src/comparator/position_loader.py:79-96` |
| **Phase 4** — Per-pair `upnl_usdt_delta`, `pos_value_delta`; excluded from `has_missing_telemetry` | Pass | `position_metrics.py:81-88, 131-132, 149-156` |
| **Phase 4** — Nine new `ValidationMetrics` fields | Pass | `apps/comparator/src/comparator/metrics.py:115-123` |
| **Phase 4** — `fold_metrics_into` aggregates via `_agg` + `pos_value_final_delta` per-side last-pair | Pass | `position_metrics.py:326-380` |
| **Phase 4** — Existing final-delta and recomputed-unrealised fields unchanged | Pass | `metrics.py:99-106`, `reporter.py:213-214` |
| **Phase 5** — Nine new `validation_metrics.csv` rows appended after existing telemetry | Pass | `reporter.py:215-226` |
| **Phase 5** — Stdout labels distinct from final-only lines | Pass | `reporter.py:472-484` |
| **Phase 5** — Six new `position_comparison.csv` columns | Pass | `reporter.py:312-317, 353-359` |
| **Tests** — All plan-listed test areas | Pass | See Unit Tests section |

---

## Findings

No blocking or non-blocking code review findings against the current working tree.

### Notes (not defects)

- **Flat-state NULL vs zero asymmetry (intentional per plan).** Live zero-row placeholders set `position_value=None` (`recorder.py:566`); backtest flat snapshots set `position_value=Decimal("0")` (`runner.py:687`). When paired, `_safe_sub` returns `None`, so flat snapshots contribute nothing to `pos_value_usdt_*` aggregates. Operators diagnosing flat-state parity should inspect per-pair CSV rows, not rely on aggregates alone.

- **Bybit field name alignment.** Live writers read camelCase `positionValue`; DB/model/reporter use snake_case `position_value`. Guard pattern `(None, "")` matches the 0056 `curRealisedPnl` path. No nested-object mismatch found.

- **Backtest notional source.** Backtest reads `tracker.state.position_value` (maintained by `calc_position_value`); live reads exchange `positionValue`. Any `avgPrice` vs computed-entry divergence is a pre-existing parity topic (see `0058_REVIEW.md`); 0059 correctly stores both sides as emitted.

- **Pre-existing guard inconsistency in WS writer.** `positionMM` / `cumRealisedPnl` still use truthy guards while `positionValue` / `curRealisedPnl` use `not in (None, "")`. Not introduced by 0059.

- **`PositionTelemetryNotMigratedError` class docstring** still says *"0034 columns are missing"* (`position_loader.py:26-29`) while module/function docs now cover 0034/0056/0059. Runtime error messages are migration-specific; optional one-line class-doc tweak only.

- **Operator step.** Post-merge migration `uv run python scripts/migrate_0059_position_value.py --database-url <url>` is documented in the plan but not automated in CI — expected for one-off schema migrations in this repo.

---

## Code Quality Review

### Correctness & data alignment

- Sign convention preserved: backtest minus live throughout (`_safe_sub`, `_agg`).
- `upnl_usdt_delta` compares stored `unrealised_pnl` 1:1 (0058 log semantics); distinct from mark-recomputed `unrealised_pnl_delta`.
- Unmatched-bt sentinel clears both new deltas to `None` (`position_metrics.py:407-408`).
- Existing CSV metric row order unchanged; nine 0059 rows appended after `cur_realised_pnl_final_delta`.
- Hedge-mode `pos_value_final_delta` cancellation tradeoff documented inline.

### Over-engineering / style

- Changes follow the 0056 pipeline pattern — no new abstractions.
- Migration script is a structural clone of `migrate_0056_cur_realised_pnl.py`.
- File sizes remain reasonable; no refactoring needed.

### Unit tests

| Area | Assessment |
|---|---|
| Schema / repository | Covered — non-NULL and explicit-NULL round-trip |
| Live WS writer | Covered — parsed value + missing field → None |
| Recorder REST | Covered — parsed `positionValue`, zero-row NULL |
| Backtest emit | Covered — in-position tracker value + flat branch `Decimal("0")` |
| Loader probe | Covered — `test_un_migrated_0059_raises` |
| Comparator deltas | Covered — upnl/pos_value aggregate, cur/cum per-snapshot `_agg`, NULL skip, no telemetry flag, unmatched marker, per-side final |
| Reporter | Covered — nine CSV rows, stdout labels, six per-pair columns |
| Isolation | Good — in-memory SQLite for probe tests; no exchange I/O |
| Naming / patterns | Matches existing 0034/0056 test conventions |

---

## Commands Run

```bash
uv run pytest -q apps/backtest/tests/test_runner.py \
  apps/comparator/tests/test_position_metrics.py \
  apps/comparator/tests/test_reporter.py \
  shared/db/tests/test_repositories.py \
  apps/event_saver/tests/test_writers.py \
  apps/recorder/tests/test_initial_rest_snapshot.py
```

Result: `287 passed in 1.01s`

```bash
uv run ruff check shared/db/src/grid_db/models.py \
  shared/db/src/grid_db/repositories.py \
  scripts/migrate_0059_position_value.py \
  apps/event_saver/src/event_saver/writers/position_writer.py \
  apps/recorder/src/recorder/recorder.py \
  apps/backtest/src/backtest/runner.py \
  apps/comparator/src/comparator/position_loader.py \
  apps/comparator/src/comparator/position_metrics.py \
  apps/comparator/src/comparator/metrics.py \
  apps/comparator/src/comparator/reporter.py
```

Result: `All checks passed!`

---

## Post-merge Checklist

1. Run `scripts/migrate_0059_position_value.py` against each active recorder DB before the next replay.
2. Re-run replay validation and confirm the nine new `validation_metrics.csv` rows and per-pair columns populate (non-zero when drift exists).
