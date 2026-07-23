# Feature 0097 — Code Review (Issue #212)

Enforce uv lockfile consistency in CI + pre-commit gate.

## Change surface

- `.github/workflows/ci.yml` — `uv sync` → `uv sync --locked` (test + lint jobs)
- `.github/workflows/risk-tier-monitor.yml` — `uv sync` → `uv sync --locked`
- `.pre-commit-config.yaml` (new) — local `uv-lock-check` hook (`uv lock --check`)
- `pyproject.toml` — `[dependency-groups] dev` += `pre-commit>=3.0`
- `uv.lock` — `pre-commit` resolved into the lock
- `.claude/rules/core-invariants.md` — new "Dependency / lockfile discipline" subsection
- `RULES.md` — index row extended with lockfile pointer

## Local behavioral verification (orchestrator)

| Check | Result |
|---|---|
| `uv lock --check` on clean tree | exit 0 |
| `uv lock --check` on real manifest drift (`tomli` added, no re-lock) | exit 1 |
| pre-commit `uv-lock-check --files pyproject.toml` on drift | **Failed** (correct message) |
| `uv sync --locked` on drift (CI equivalent) | exit 1 |
| post-restore `uv lock --check` | exit 0 |
| `files:` regex vs 12 members + 2 bbu paths + nested | root & members match; `bbu_reference/**` and nested excluded |
| `pre-commit` present in `uv.lock` | yes (3 refs) |

## External review trail

- **Engines:** codex + cursor (both, read-only), 1 iteration.
- **codex:** `NO P1/P2 FINDINGS`; confirmed plan conformance, no P3. (Prior
  contamination issue — reviewing an unrelated project — resolved by
  explicit repo anchoring in the prompt.)
- **cursor:** `NO P1/P2 FINDINGS`; 13-point verification log, all MATCH.
  One P3: `.pre-commit-config.yaml:11` `files:` omits `uv.lock`, so a
  lock-only hand-edit commit skips the local hook — **intentional per
  plan** (CI `uv sync --locked` is the backstop); residual local gap only,
  not fixed.
- **Findings:** raised 1 (P3), accepted-as-designed 1, rejected 0, P1/P2 0.
- **Fixes applied:** none required.

## Result

Zero valid P1/P2. No Python logic changed → no unit tests warranted (gate
is self-testing via CI + pre-commit hook; plan Verify is manual). Ready to
commit.

## Reminder on commit

`git add` **all** of: the 6 tracked edits, the new `.pre-commit-config.yaml`,
and `docs/features/0097_PLAN.md` + this `0097_REVIEW.md`. After pulling this
change, run `uv run pre-commit install` once per clone to activate the hook.
