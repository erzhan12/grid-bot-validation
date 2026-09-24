# 0109 — Code Review

Feature: status-file consumer contract + cron watchdog (issue #258, Phase 1).
Plan: `docs/features/0109_PLAN.md`.

## Local staged review (`/review-fix-loop-staged`)

1 iteration, 5 parallel reviewers (quality, security, performance, testing, docs).
0 criticals remaining.

- Rejected: "slice-4 mutation probes missing" (flagged CRITICAL) — the plan defines them
  as temporary manual probes, run and restored; the implementer ran both (STARTING
  dropped from `_OK_STATES` → round-trip STARTING case failed; freshness loosened to
  2× → `NOW+61s → STALE` case failed).
- Downgraded to INFO: escaping `--path` in `format_line` — the path is an
  operator-set literal in cron, not untrusted input.
- Fixed (WARNING): subprocess `__main__` test now asserts exactly one stdout line and
  empty stderr.

## External review trail (`/ext-code-review`)

Engines: codex (gpt-5.6-sol, read-only) + cursor agent (ask mode). 3 rounds.

| Round | Raised | Accepted (fixed) | Rejected | P3 skipped |
|---|---|---|---|---|
| 1 | 5 | 4 | 0 | 1 (dup) |
| 2 | 4 | 3 | 1 | 0 |
| 3 | 2 | 1 | 0 | 1 |

Accepted and fixed:
- R1 P2 — runbook step-5 commands used `$STATE_DIR`, which exists only inside
  `watchdog.sh`; operator shell would expand to `/status.throttle`. Step 5 now sets
  `STATE_DIR` in the operator shell first.
- R1 P3 — grace counter `$((grace_count + 1))` aborts `set -e` on `08`/`09` (octal)
  or oversized values. Now `10#` base + length guard; smoke-tested with `08`,
  20-digit value and `abc`.
- R1 P3 — `TestProducerRoundTrip` docstring referenced nonexistent
  `test_mutation_probe_*` tests; reworded.
- R1 P3 — no test that a custom `--max-age-seconds` reaches `check_status`; added
  `test_main_custom_max_age_reaches_check_status`.
- R2 P2 — runbook said to paste the whole block into the RUNNING branch, but the
  non-RUNNING counter-reset lines need `STATE_DIR`; prose now places the settings
  part at the top of the script and only the logic inside RUNNING.
- R2 P3 — `test_main_default_path` now also pins the default `max_age_seconds=60`;
  the missing-file CLI test asserts the `state=- age=-s` placeholders.
- R3 P3 — `.claude/rules/gridbot.md` cross-reference "see `HealthStatusWriter.write`
  above" pointed at nothing; now points at `health.py`.

Rejected:
- R2 P3 — `[ "$up" -lt ... ]` treats zero-padded values as octal: false, the `[`
  builtin compares in decimal (`[ 08 -lt 360 ]` is true).

Skipped P3: stub-based `TestMain` tests do not re-assert the one-line stdout contract
(already covered by the verdict-producing `TestMain` cases).

## Snippet smoke test

The `watchdog.sh` extension snippet was run locally under `set -euo pipefail` with
stubbed Telegram/throttle/`supervisorctl` against the real CLI: fresh → silent;
stale → one alert then throttled; missing + non-numeric uptime → alert; 3 in-grace
runs → one `restart_loop` alert; broken module → stderr text becomes the alert.
`ps -o etimes=` (Linux procps) could not be exercised on macOS — runbook step 1
pre-checks it on the VPS.

## Final status

`uv run pytest apps/gridbot/tests -q` → 966 passed. `make lint` → clean.
