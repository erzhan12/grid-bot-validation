# Feature 0060 Code Review

**Plan:** `docs/features/0060_PLAN.md`  
**Scope:** Backtest snapshot `position_value` mark-based parity; docstring/comment cleanup; regression tests; `RULES.md` semantics  
**Review pass:** 2 (post-fix)

---

## Verdict

**Approve.** Implementation matches the locked design: only the **reported snapshot** `position_value` becomes mark-based; margin, risk-side `PositionState`, live writers, and comparator logic are unchanged. Pass-1 polish items are resolved; no blocking or non-blocking defects remain.

---

## Resolved From Prior Review

| Pass-1 ID | Resolution |
|---|---|
| N1 — Margin guard uneven across Stage 5 cases | Fixed — `in_profit_long` and `underwater_short` now capture `entry_im` / `entry_mm` and assert both unchanged (`test_runner.py:242-255, 267-281`). |
| N2 — Stale `# 0059` on snapshot construction | Fixed — `runner.py:705` → `# 0059/0060`. |
| N3 — ORM comment omitted `\|size\|` | Fixed — `models.py:398` → `\|size\| * mark_price`. |
| N4 — Stale `test_repositories` comment | Fixed — `test_repositories.py:1587` → opaque round-trip wording. |

---

## Plan Implementation Check

| Stage | Status | Evidence |
|---|---|---|
| **1** — Emit-site split (mark snapshot, entry margin) | Pass | `apps/backtest/src/backtest/runner.py:676-678` — `position_value = abs(size) * mark_price`; `_update_margin` / `position_tracker.py:297-298` untouched |
| **1** — Docstring update on `_emit_position_snapshot` | Pass | `runner.py:632-634` |
| **1** — Flat branch explicit zero | Pass | `runner.py:684-688` (unchanged) |
| **1** — No mark fallback to entry | Pass | No `avg_entry_price` fallback added |
| **2** — `calc_position_value` docstring only | Pass | `packages/gridcore/src/gridcore/pnl.py:134-153` — body `return size * entry_price` unchanged |
| **3** — Stale semantic comments (required sites) | Pass | `models.py:398-400`, `position_metrics.py:85-86`, `recorder.py:531`, `position_writer.py:255`, `pnl_checker/calculator.py:57` |
| **3** — Live writer **behavior** unchanged | Pass | Still `Decimal(str(pos.get("positionValue")))` with `not in (None, "")` guard |
| **4** — Update existing snapshot tests | Pass | `test_emit_position_snapshot_sets_position_value` decoupled from tracker coupling |
| **5** — Regression tests (long underwater/in-profit, short, parity) | Pass | `test_runner.py:207-344` |
| **5** — Margin guard on all mark≠entry cases | Pass | Underwater long, in-profit long, underwater short each assert `initial_margin` and `maintenance_margin` unchanged |
| **6** — `RULES.md` bullets | Pass | `RULES.md:1751`, `RULES.md:2044` |
| **Out of scope** — No margin / live / comparator / migration / bbu edits | Pass | Diff: 11 files (`git diff --stat`) |

---

## Findings

No blocking or non-blocking code review findings against the current working tree.

### Notes (not defects)

- **`abs(size)` at emit site.** Per-leg trackers always hold positive `size` (`position_tracker.py`); `abs()` matches Bybit `\|size\| × mark` wording and is harmless defense-in-depth.
- **Risk path unchanged.** `_build_position_state` still uses `calc_position_value(size, entry_price)` (`runner.py:734`); DEBUG margin log still uses `tracker.state.position_value` (`runner.py:567-570`).
- **Data alignment.** Live: camelCase `positionValue` → snake_case `position_value`. Backtest snapshot: mark from `_last_mark_price` else `event.price` (`runner.py:602-606`), same as existing `mark_price` / unrealised columns. No nested-object mismatch.
- **Flat-state asymmetry (pre-existing).** Live zero-rows → `position_value=None`; backtest flat → `Decimal("0")`. Unchanged by 0060; flat pairs still yield `pos_value_delta=None` via `_safe_sub`.
- **Historical rows.** Pre-0060 backtest DB rows remain entry-based; plan documents forward-only fix and no backfill.
- **Cross-package test import.** `test_snapshot_position_value_mark_parity_with_live_style_notional` imports private `_build_pair` from comparator — acceptable for proving `pos_value_delta == 0` end-to-end; no production coupling added.
- **Full-suite noise.** `uv run pytest -q` reports 3 failures in `apps/backtest/tests/test_risk_limit_info.py` (cache/lock tests). Those files are **not** in the 0060 diff; failures are unrelated to this feature.

---

## Code Quality Review

### Correctness

- Core fix is a single expression at the snapshot chokepoint; cannot accidentally repoint `_update_margin` or `calc_initial_margin`.
- Deliberate omission of entry fallback when `mark_price` is bad preserves “visible wrong signal” per plan (consistent with `unrealised_pnl` / `mark_price` columns).
- Blast-radius grep: no unexpected readers of snapshot `position_value` were modified.

### Over-engineering / style

- Minimal diff (~177 lines, mostly tests). No new helpers or abstractions.
- Matches 0059/0056 comment-tag conventions (`0059/0060:`).
- `ruff check` clean on changed sources.

### Unit tests

| Area | Assessment |
|---|---|
| Mark == entry (no drift) | Covered — `test_emit_position_snapshot_sets_position_value` |
| Long underwater / in-profit | Covered — mark ≠ entry, snapshot vs tracker split |
| Short tracker path | Covered — `DirectionType.SHORT`, `side == "Sell"` |
| Margin unchanged (finding 3) | Covered — IM + MM on all three mark≠entry cases |
| Comparator delta ≈ 0 | Covered — synthetic live `abs(qty)*mark` vs backtest emit |
| Flat branch | Covered — `test_emit_position_snapshot_flat_branch_zero` |
| Repository round-trip | Comment aligned — opaque storage semantics |
| `test_pnl.py` | Unchanged values; comment only |
| Isolation | Good — direct tracker + `_emit_position_snapshot`; no exchange I/O |
| Naming | Consistent `test_snapshot_position_value_*` / `0060` docstrings |

---

## Commands Run

```bash
uv run pytest apps/backtest/tests/test_runner.py packages/gridcore/tests/test_pnl.py apps/comparator/tests -q
```

Result: **340 passed** in 0.38s

```bash
uv run pytest -q
```

Result: **2330 passed**, 3 skipped, **3 failed** (`test_risk_limit_info.py` — unrelated to 0060)

```bash
uv run ruff check apps/backtest/src/backtest/runner.py packages/gridcore/src/gridcore/pnl.py \
  apps/backtest/tests/test_runner.py shared/db/src/grid_db/models.py shared/db/tests/test_repositories.py
```

Result: All checks passed

---

## Acceptance / Operator Follow-up

- **Unit acceptance:** Mark-based snapshot + entry-based margin split is proven by tests above.
- **E2E (optional per plan):** Re-run backtest on a window with underwater/in-profit positions and confirm `pos_value_delta` ≈ 0 in comparator output. Use a **fresh** backtest DB (no backfill of old rows).
- **Commit:** Not performed in this review; request explicitly per workflow when ready.
