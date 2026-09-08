# External review trail — `docs/deploy/vps_plan.md`

Target reviewed: the VPS deployment runbook (live `mainnet_live` gridbot → DigitalOcean).
This is a plan/runbook, not application code — no test/lint gate applies; verification was
reading the cited plan lines and the referenced gridbot source.

## Round 1 (2026-09-07)

- **Engines:** Cursor agent CLI (`agent -p --mode ask`, read-only) + a Claude **Fable 5**
  subagent (substituted for codex at the user's request). Both run read-only; all edits by the
  orchestrator.
- **Criteria:** `commands/code_review.md`, adapted for a deployment runbook (config correctness,
  live-money cutover safety, SSH/secret security, operational gaps, internal consistency).

### Findings raised, triaged, and resolved

Convergence was strong: both engines independently flagged the grid-continuity P1 and the
signal/liveness/rollback P2 cluster.

| ID | Severity | Finding | Source | Verdict | Fix |
|----|----------|---------|--------|---------|-----|
| P1-A | P1 | `autostart=true` + staged `shadow_mode:false` → a reboot could start a **second live bot** on the account before the cutover STOP gates | cursor#1 | ACCEPT | `autostart=false` until Phase 8; stage `shadow_mode:true` from first write (Phase 3/4/8) |
| P1-B | P1 | No `db/grid_anchor.json`/DB migration step → VPS builds a fresh grid; first ticker cancels reconcile-adopted live orders (`outside_grid`/`side_mismatch`); shadow dry-run pollutes VPS grid state (save is NOT shadow-gated, `runner.py:689`); verify runs pre-first-tick so it passes right before the cancels | cursor#2,#6 + fable F1 | ACCEPT | Rewrote Phase 6/7: wipe shadow state, copy laptop `grid_anchor.json`, verify prices AFTER first ticks; added rollback Phase 7R |
| P2-C | P2 | supervisor `stdout_logfile`==`stderr_logfile` → two independent 50MB rotators corrupt output | fable F2 | ACCEPT | `redirect_stderr=true` + single `stdout_logfile` + maxbytes/backups |
| P2-D | P2 | `exec uv run python` interposes `uv`; SIGINT may not reach the graceful handler → SIGKILL a live bot at `stopwaitsecs` | cursor#7 + fable F3 | ACCEPT | wrapper `exec .venv/bin/python`; `stopasgroup/killasgroup=true` |
| P2-E | P2 | No liveness alerting; `autorestart` goes FATAL after `startretries` (0086 exit 1) silently | cursor#8 + fable F4 | ACCEPT | Added required watchdog (eventlistener/cron) + require Telegram config |
| P2-F | P2 | No rollback path | cursor#8 + fable F5 | ACCEPT | Added Phase 7R (reverse cutover, same ordering guard) |
| P2-G | P2 | Analyzer reads a single `--log`; no `.1/.gz` handling vs daily rotation → health windows spanning rotation lose coverage, corrupt durable ledgers | fable F6 | ACCEPT | Phase 4 note: mandatory hourly sampler + read rotated files / weekly rotation |
| P2-H | P2 | "leaked health key → useless" is false (still dumps trading log, writes state) | cursor#3 + fable F12 | ACCEPT | Reworded; `restrict` + `from=` + ownership; noted `no-user-rc` covered by `restrict` |
| P2-I | P2 | ".env never read" is policy, not an enforced control | cursor#4 | ACCEPT | Reworded to operational enforcement (create in SSH tty only) |
| P2-J | P2 | root `conf/gridbot.yaml` is NOT gitignored → dirty tree / accidental live-config commit | fable F9 | ACCEPT | Phase 3 note: gitignore it or store outside repo |
| P2-K | P2 | Bybit API key IP allowlist would reject the new SGP1 IP → 0086 abort | cursor#14 | ACCEPT | Phase 0 prereq + open item |
| P2-L | P2 | Hourly sampler cron user/TZ unstated → `health_state.json` perms + mixed samples | cursor#9 + fable F11 | ACCEPT | `USER` crontab + `CRON_TZ=UTC` + `flock` |
| P3s | P3 | copytruncate line-loss window; `console.log` rotation; bare `uv run` re-sync; `health_state.json` concurrent write; sampler phase-ordering; durable `status_file_path`; missing `git`/`uv python install` | both | ACCEPT (cheap) / NOTE | Folded into the above edits + Open items |

### Rejected

- **cursor#5 — "shadow not safe to run alongside the live bot."** REJECT as a distinct safety
  finding. Fable independently verified (verification #5) that place/cancel/amend/cancel_all are
  early-returned under `shadow_mode` (`executor.py:313,417`) — grep for exchange-mutating calls in
  the shadow path returned zero hits; reconcile/WS are read-only. The claim's only valid kernel
  (shadow writes the VPS's own `grid_anchor.json`) is captured in P1-B. Residual is minor
  rate-limit pressure from a second private-WS/REST session on one key — downgraded to an in-line
  P3 note in Phase 6.

### Verification status

- No code changed — edits are confined to `docs/deploy/vps_plan.md` (+ this trail). `make test` /
  `make lint` not applicable to a docs-only change.
- Referenced-code claims verified during triage: `--config`/`--log-file` flags
  (`main.py:175-188`); `shadow_mode` per-strategy field (`config.py:397`, `orchestrator.py:897`);
  `_persist_grid_state` not shadow-gated (`runner.py:689`); one-tick reconcile adoption
  (`reconciler.py:124-138`); `db/` + `db/grid_anchor.json` gitignored (`.gitignore:68-69`).

### Result

Round 1: **12 valid P1/P2 accepted and fixed in the plan, 1 rejected with evidence, P3s folded
in.** All accepted findings resolved by plan edits. A second external round on a now-substantially-
rewritten runbook would mostly surface fresh doc nits (convergence guard) — recommend a human read
of the revised Phases 3–8 instead. Re-run `/ext-code-review docs/deploy/vps_plan.md` on request.
