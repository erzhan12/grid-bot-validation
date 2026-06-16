# 0075 — Code Review: Include `apps/backtest/tests` in `make test` (issue #178)

Reviewed change: `Makefile` (+1 line) and `RULES.md` (note extended). Branch
`worktree-feature+0075-backtest-make-test`. Reviewed against `0075_PLAN.md` and
the 6-point review rubric.

## Verdict

**APPROVE.** Plan correctly and fully implemented. No bugs, no scope creep. `make
test` is green (exit 0, all suites incl. backtest 507 passed). No code review
findings block merge.

## 1. Plan correctly implemented?

Yes — all 4 phases done:

| Phase | Plan requirement | Status |
|-------|------------------|--------|
| 1 — Triage gate | Run `apps/backtest/tests`, expect 507 passed | ✅ 507 passed; no source fixes needed |
| 2 — Wire Makefile | Insert `uv run pytest apps/backtest/tests --cov=backtest --cov-append -q`, TAB-indented, before `tests/integration` | ✅ line 18, TAB confirmed, before integration |
| 3 — Verify full target | `make test` green, backtest collected, coverage rows in TOTAL | ✅ exit 0; TOTAL 8593→10851 after backtest run |
| 4 — RULES.md note | 1–2 line note: covers all testpaths incl. backtest, prior omission unjustified | ✅ extended existing `make test` note |

Flag string matches acceptance criteria verbatim (`--cov=backtest --cov-append -q`).

## 2. Obvious bugs / issues

None.
- TAB indentation verified (`grep -P '^\t'`) — the Makefile spaces-vs-tabs pitfall
  called out in the plan is avoided.
- Placement is after `apps/replay/tests`, before `apps/pnl_checker/tests` and the
  final `tests/integration/` line — so `--cov-report=term-missing` stays last and
  the merged TOTAL includes backtest. Empirically confirmed (backtest rows appear
  in final coverage report).
- `--cov=backtest` resolves: `apps/backtest/src` is on `pythonpath` and
  `apps/backtest/src/backtest/` is the package. No `ModuleNotFound`.

## 3. Data alignment issues

N/A — change is a Makefile recipe line + a markdown note. No serialization,
casing, or nesting surface.

## 4. Over-engineering / file size

None. Minimal 2-line surgical change. New backtest line mirrors the exact
`uv run pytest <dir> --cov=<pkg> --cov-append -q` form of the 8 sibling lines.

## 5. Weird syntax / style mismatch

None. New Makefile line is byte-for-byte consistent with the surrounding recipe
lines (same flags, same order of `--cov`/`--cov-append`/`-q`). RULES.md note
appended to the existing `make test` note rather than creating a new section —
consistent with the "don't over-complicate RULES.md" guideline.

## 6. Unit tests

No new application code was added, so no new unit tests are required. The change
*is* test wiring: it causes 16 backtest test files / 507 existing tests to run
under the CI gate that previously skipped them. Those tests already exist, follow
project conventions (`test_*.py`, per-package `tests/`, `conftest.py` present),
and pass (507/507) under both standalone and full `make test` runs.

- Acceptance criteria are verified by executing `make test`, not by a new
  meta-test. A test asserting "the Makefile contains the backtest line" would be
  over-engineering (no precedent for testing the Makefile in this repo) — correctly
  omitted.

## Observations (non-blocking, no action required)

- **O-1 (info).** `make lint` reports 99 pre-existing errors (tracked separately as
  issue #180; was 184). This change touches zero `.py` files, so it adds none. The
  `test` CI job (this fix's target) is independent of the `lint` job. Out of scope
  for #178.
- **O-2 (info).** Adding backtest coverage moves the merged TOTAL (now ~92% on the
  appended run vs the ~73% cited in the Makefile comment). No `--cov-fail-under` on
  the merged run, so nothing breaks — expected per plan §Risks. The stale ~73%
  figure in the Makefile/RULES.md comment was not updated; left untouched as a
  surgical-change discipline (pre-existing, not introduced here).

## Conclusion

Implementation matches the plan exactly, satisfies all four issue #178 acceptance
criteria, and leaves `make test` green. Ready to commit.
