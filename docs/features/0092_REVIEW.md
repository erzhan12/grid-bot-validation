# 0092 — Code review trail (coverage gate, issue #214)

Change surface: `Makefile`, `RULES.md`, `packages/gridcore/README.md` (docs +
Makefile only; no Python touched).

## Internal review (5-category subagent pass, 1 iteration)

- Quality / Security / Performance / Documentation: no findings.
- Testing raised 1 WARNING + 2 INFO:
  - **REJECTED** — "`coverage report --fail-under=88` exits 0 with no
    `.coverage` → silent gate bypass": verified empirically, `No data to
    report.` exits **1**, so `make test` fails (fail-closed). Also, a missing
    pytest-cov would make `--cov=gridcore` an unknown option → pytest exit 4 at
    the first invocation.
  - INFO (accepted gap) — integration run's inert cov flags could confuse a
    future edit; mitigated by the Makefile comment + RULES.md "Coverage gate"
    subsection.
  - INFO (accepted gap) — no automated regression test for the gate itself;
    manual breach verification performed (see below).

## External review trail (ext-code-review)

- Engines: codex (read-only exec) + cursor agent (ask mode), 1 round each.
- Both: **NO P1/P2 FINDINGS**. Cursor emitted a 16-row verification log, all
  PASS.
- 1 deduped P3 (raised by both): `packages/gridcore/README.md:14` Key Features
  still said "88% test coverage" — outside the plan's listed line fixes but
  contradicting the updated 94.7% numbers. **ACCEPTED + FIXED** (→ 94.7%).
- Rejected/pre-seeded items (not re-raised): no-data gate bypass (refuted,
  exit 1), inert-flags fragility (deliberate + documented), gate meta-test
  (accepted gap).

## Rebase note

Rebased onto `acba155` (RULES.md stale-entry sweep, merged to main mid-review).
Conflict resolution: kept the new "Coverage gate" subsection; did NOT restore
the swept "Development Workflow" section (duplicated in parent CLAUDE.md) and
did NOT re-add a `**Coverage**: NN%` percentage to the gridcore header — the
sweep deliberately dropped rotting undated percentages. Dated numbers inside
the gate rationale (≈91% / 94.7% as of 2026-07-17) remain.

## Verification status

- `make test` exit 0 — gridcore 94.74% ≥ 80, TOTAL 91% ≥ 88.
- Breach checks: `coverage report --fail-under=99` → exit 2 with
  `Coverage failure: total of 91 is less than fail-under=99`; gridcore
  `--cov-fail-under=99` → exit 1 with `FAIL Required test coverage of 99% not
  reached. Total coverage: 94.74%`.
- No-data check: `coverage report --fail-under=88` without `.coverage` →
  `No data to report.`, exit 1 (fail-closed).
- `make lint` exit 0.
