# Deploy plan — live gridbot → DigitalOcean VPS

**Goal:** run the live `mainnet_live` gridbot 24/7 on a VPS (no laptop sleep / reboot / `/tmp` wipe / travel-network gaps), while keeping on-demand `/gridbot-health` log analysis working exactly as it does locally.

**Status:** DRAFT — not yet executed. Real money (`mainnet_live`, LTCUSDT + SOLUSDT). Resolve the deployment-artifact prerequisites in Phase 5 before provisioning; cutover remains STOP-gated.

## Decisions locked
- **Host:** new, separate DO droplet (isolate live bot from the `trad_save_history` collector — separate blast radius). Ubuntu 24.04 LTS, 1 vCPU / 1 GB, region **Singapore (SGP1)** (near Bybit → lower latency than laptop).
- **Process manager:** `supervisord` (native git + `uv`, not Docker).
- **Log rotation:** keep **7 days**, daily, `copytruncate`.
- **Claude access:** dedicated SSH key + **forced-command** in `authorized_keys` (key can ONLY run `analyze.py`, nothing else). Separate from the deploy key.

## Architecture

```
Laptop (Claude Code)                     VPS (24/7, Singapore)
  /gridbot-health  ──ssh(health key)──►  run-analyze.sh → analyze.py
                                            reads /home/USER/gridbot/gridbot.log
                   ◄──── stdout ─────────   writes health_state.json (beside analyze.py)
                   (tables 1-15)
```
- analyzer + `health_state.json` live on the VPS next to the bot. Claude SSHes in, runs the analyzer remotely, only markdown tables come back. The 5+ GB log never leaves the VPS.
- The bot writes the JSON-lines log itself via `--log-file` (Python `FileHandler`, append mode) — NOT a shell redirect and NOT supervisor stdout capture. Supervisor stdout captures only the human-readable console.

## Security — SSH key never enters Claude's prompt
- The **private** SSH key is a file on the laptop (`~/.ssh/`). `ssh` uses it internally (crypto handshake); it is never printed to stdout, so it never enters the model context. Only the command string (`ssh gridbot-vps '...'`) and its stdout (health tables — trading data, same as today) go to the model.
- "Claude never reads `.env`/keys" is an **operating policy, not an enforced control** — a `! cat .env` or a Read of the file would put secrets into context. Enforce it operationally: create `/home/USER/gridbot/.env` only in an interactive SSH tty (`nano`/`install -m 600`) on the droplet, never via a command whose output returns to Claude.
- `BYBIT_API_KEY` / `BYBIT_API_SECRET` live in `/home/USER/gridbot/.env` (`chmod 600`), read by the bot via env — not printed by the bot, and kept out of context by the policy above.
- **Forced-command** restricts the health key to running the analyzer — it cannot place/cancel orders or run arbitrary commands. It is NOT "useless if leaked": a leaked key still exposes the live trading log and writes `health_state.json`. `from="<laptop-ip>"` (Phase 5) limits where it can be used from.

---

## Phases

Legend: 👤 = user does · 🤖 = Claude does (over SSH / locally) · 🛑 = STOP gate (wait for explicit confirmation)

### Phase 0 — Droplet
- 👤 Create new DO droplet: Ubuntu 24.04 LTS, 1 vCPU / 1 GB, SGP1.
- 👤 Give Claude: droplet IP + sudo (non-root) username.
- 👤 Base hardening (can defer): `ufw`, `fail2ban`.
- 👤 **Bybit API IP allowlist:** if the `mainnet_live` API key is IP-restricted (check Bybit → API Management), add the new SGP1 droplet IP to the key's allowlist BEFORE Phase 6. Otherwise the bot's first authenticated call fails and 0086 fail-closed aborts startup. Reserve/note a static IP for the droplet so it survives reboots.
- ⚠️ **Disk:** the JSON log grows ~5 GB+ before rotation on a 1 GB droplet — the 7-day `rotate`+`compress` policy (Phase 4) must be in place before the bot runs for long. Add DO monitoring / an `ncdu` cron or a disk-usage alert; a full disk stalls both the bot's log writes and the analyzer.

### Phase 1 — Deploy SSH key + access
- 👤 Generate a **dedicated** ed25519 key on the laptop for this (not the main key). Private stays on the laptop.
- 👤 Copy the **public** part to the droplet's sudo user (normal access for deploy).
- 👤 Add `~/.ssh/config` alias `Host gridbot-vps` → IP + `IdentityFile`.
- ✅ Check: `ssh gridbot-vps whoami` works passwordless.
- _(Forced-command is set up in Phase 5, after code + analyzer are on the box.)_

### Phase 2 — Bot code on the droplet
- 👤/🤖 `git clone` repo to `/home/USER/gridbot`.
- 🤖 Install `uv`, then `uv sync --locked` (lockfile discipline).
- 🤖 `uv run python -m gridbot.main --help` → confirm the `--log-file` and `--config` options from `apps/gridbot/src/gridbot/main.py`.

### Phase 3 — Secrets
- 👤 Create `/home/USER/gridbot/.env` (`chmod 600`, NOT in git): `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `DATABASE_URL`. User enters keys (via `! …` or on the droplet) — Claude never prints/reads them.
- 👤 Create `/home/USER/gridbot/conf/gridbot.yaml` (`chmod 600`, NOT in git) from a reviewed live configuration. `conf/gridbot_test.yaml` is a local ignored file; do not assume it is included in `git clone` or suitable for the VPS.
  - ⚠️ **`conf/gridbot.yaml` at repo root is NOT gitignored** (only `conf/gridbot_test.yaml` and `apps/gridbot/conf/gridbot.yaml` are). Placing it under the cloned repo's `conf/` leaves a permanently dirty tree and risks accidentally committing live config. Either add `conf/gridbot.yaml` to `.gitignore` on the VPS clone, or store the config OUTSIDE the repo (e.g. `/home/USER/gridbot-etc/gridbot.yaml`) and point `--config` there.
  - ⚠️ **Stage with `shadow_mode: true` for both strategies from the FIRST write.** The staged file must never contain `shadow_mode: false` until the Phase 7 cutover — combined with `autostart` (Phase 4) a reboot could otherwise launch a second LIVE bot on the account before the STOP gates. Flip to `false` only at cutover.
- 🤖 Wrapper `run-gridbot.sh` (`chmod 700`):
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  cd /home/USER/gridbot
  set -a; source .env; set +a
  # Direct venv python (built by Phase 2 `uv sync --locked`) — NOT `uv run`.
  # `uv run` re-syncs on every restart (network dependency in the restart path,
  # violates --locked discipline) and interposes the `uv` process between
  # supervisord and python, so SIGINT may not reach the graceful handler in
  # main.py (see Phase 4 stopsignal).
  exec /home/USER/gridbot/.venv/bin/python -m gridbot.main \
    --config /home/USER/gridbot/conf/gridbot.yaml \
    --log-file /home/USER/gridbot/gridbot.log
  ```

### Phase 4 — Supervisor + rotation
- 🤖 Install supervisor.
- 🤖 `/etc/supervisor/conf.d/gridbot.conf`:
  ```ini
  [program:gridbot]
  command=/home/USER/gridbot/run-gridbot.sh
  directory=/home/USER/gridbot
  user=USER
  autostart=false
  autorestart=true
  startretries=3
  startsecs=30
  stopsignal=INT
  stopwaitsecs=30
  stopasgroup=true
  killasgroup=true
  redirect_stderr=true
  stdout_logfile=/home/USER/gridbot/console.log
  stdout_logfile_maxbytes=50MB
  stdout_logfile_backups=5
  ```
  - `autostart=false` — the bot must NOT auto-launch on supervisor install/reboot before the Phase 7 cutover (prevents a second live bot on the account). Every pre-cutover start is a manual `supervisorctl start gridbot`. Flip to `autostart=true` only AFTER cutover so it survives reboots (Phase 8).
  - `redirect_stderr=true` + a single `stdout_logfile` — do NOT point `stdout_logfile` and `stderr_logfile` at the same path; supervisord opens two independent handles with two independent rotators on one file → interleaved/corrupted output and rotation clobbering.
  - `stopsignal=INT` + `stopwaitsecs=30` → clean Ctrl-C-equivalent graceful shutdown. `stopasgroup`/`killasgroup` ensure the signal reaches the whole process group (belt-and-suspenders with the direct-`python` wrapper).
  - JSON log (for the analyzer) is written by the bot itself to `gridbot.log`.
- 🤖 **Liveness watchdog (required — the bot is unattended real money).** `autorestart=true` goes **FATAL after `startretries` failed starts** — exactly what 0086 fail-closed (`exit 1`) produces during a sustained Bybit/network outage — leaving positions unmanaged silently until the next manual `/gridbot-health`. Add one of: (a) a supervisord `eventlistener` on `PROCESS_STATE_FATAL`, or (b) a cron liveness check (process up AND `gridbot.log` mtime fresh within N minutes) that alerts to an EXTERNAL channel. Also require Telegram `Notifier` config in the live YAML (it is a no-op if unconfigured, and dies with the process, so it cannot be the only alarm).
- 🤖 `logrotate` `/etc/logrotate.d/gridbot`: `gridbot.log` — `daily`, `rotate 7`, **`copytruncate`** (safe with Python append-mode `FileHandler` — writes always at EOF, no sparse holes), `compress`, `missingok`, `notifempty`.
  - ⚠️ **Analyzer must read rotated files.** `copytruncate` has an inherent copy→truncate window where a few concurrently-written JSON lines can be lost (acceptable — document it). More important: the deployed `analyze.py` reads a single `--log` path with no `.1`/`.gz` handling; locally the log was never rotated so "window since last invocation" always had full history. On the VPS, any health window spanning a daily rotation (travel = the stated use case → checks less than daily) silently loses coverage and corrupts the durable cross-restart ledgers that assume gap-free parsing. Mitigate by BOTH: (a) the mandatory hourly `--sample-only` cron below (keeps the durable ledgers current regardless of rotation), and (b) either teach the deployed analyzer to also read `gridbot.log.1[.gz]` when the cursor predates the current file, or switch to weekly/size-based rotation. `console.log` is capped by supervisord's own `stdout_logfile_maxbytes`/`_backups` (Phase 4 conf) — no separate logrotate needed.
- 🤖 **Hourly profit sampler** (moved off the laptop launchd agent): after Phase 5 provisions the reviewed VPS health artifact, install as the **`USER` crontab** (NOT root — else `health_state.json` becomes unwritable by the health SSH user) with `CRON_TZ=UTC` pinned (so migrated laptop samples are not mixed across timezones): `CRON_TZ=UTC` then `0 * * * * flock -n /home/USER/gridbot-health/.sample.lock /usr/bin/python3 /home/USER/gridbot-health/analyze.py --log /home/USER/gridbot/gridbot.log --sample-only` → even 1-hour `activity` cadence for `--profit-chart` / `--csv`, and keeps the durable ledgers gap-free across rotations. `flock` guards against the cron sampler and an on-demand SSH invocation doing a concurrent unlocked read-merge-write of `health_state.json` (add the same `flock` in `run-analyze.sh`, Phase 5). On the VPS this is robust (no sleep/reboot). Retire the laptop `com.gridbot.health-sampler` launchd agent at cutover.
  - _(This bullet depends on the Phase 5 artifact — execute it during Phase 5, not before.)_
- ⚠️ Do NOT start the bot live here — next phase is a shadow dry-run.

### Phase 5 — Forced-command health channel + rewire skill
- 👤 Generate a **second** key `gridbot-health` (Claude's monitoring channel, separate from the deploy key).
- 🛑 **Prerequisite:** `gridbot-health` is a local, gitignored artifact; `git clone` does not provide it. Create or select a reviewed, versioned deployment artifact before copying it to `/home/USER/gridbot-health`. It must not retain the laptop's absolute config path, must read the VPS log path, and must keep `health_state.json` beside the deployed analyzer.
- 🤖 `run-analyze.sh` on the droplet (`chmod 700`, owned by `USER`, not group/other-writable — a writable forced-command script defeats the restriction):
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  exec flock -n /home/USER/gridbot-health/.sample.lock \
    /usr/bin/python3 /home/USER/gridbot-health/analyze.py \
    --log /home/USER/gridbot/gridbot.log
  ```
- 👤 In the droplet's `~/.ssh/authorized_keys`, for the health key (use `restrict` — it disables ALL current and future forwarding/pty/user-rc features, superset of the individual `no-*` flags including `no-user-rc`, which the individual list omits):
  ```
  restrict,from="<laptop-ip-or-cidr>",command="/home/USER/gridbot-health/run-analyze.sh" ssh-ed25519 AAAA...gridbot-health
  ```
  → this key can ONLY run the no-argument health check, and only from the laptop's IP.
- ⚠️ **Leaked health key is NOT "useless".** The forced command still runs `analyze.py` against the live JSON log (full trading history) and writes `health_state.json`. Treat the key as **read access to trading data**. `from=` limits the blast radius; the value is that it cannot place/cancel orders or run arbitrary commands — not that a leak is harmless.
- 🤖 If remote analyzer options are required, add a strict allowlist for exact `SSH_ORIGINAL_COMMAND` forms to `run-analyze.sh`; do not use `eval` or pass arbitrary SSH input to a shell. Until then, the local invocation is exactly `ssh gridbot-health-vps` (alias bound to the health key). `health_state.json` lives on the droplet beside `analyze.py`. Tables identical for the user.
- ✅ Check: `/gridbot-health` over SSH returns tables.

### Phase 6 — Shadow dry-run (validate WITHOUT real orders) 🛑
- 🤖 Confirm `/home/USER/gridbot/conf/gridbot.yaml` still has `shadow_mode: true` for both strategies (it was staged that way in Phase 3).
- 🤖 `supervisorctl start gridbot`. Shadow = intents logged, order place/cancel/amend are early-returned in the executor → **no exchange mutations** (verified: `executor.py:313,417`). Reconcile and WS reads are non-mutating, so it is safe alongside the live laptop bot for order-book purposes. (Minor: a second private-WS + REST-poll session on the same key adds rate-limit pressure — keep the dry-run bounded, don't leave it running for days.)
- ⚠️ **Shadow WILL write VPS grid state.** `_persist_grid_state` is NOT shadow-gated (`runner.py:689`): the dry-run builds a fresh grid from market price and saves it to the droplet's `db/grid_anchor.json`. This pollutes the state that Phase 7 relies on — Phase 7 step 3 wipes it before cutover.
- 🤖 Verify over SSH: startup reconcile OK, JSON log writing, `/gridbot-health` works, Bybit latency from Singapore.
- 🛑 STOP — show dry-run results. Cutover only after user OK.

### Phase 7 — Cutover to live (STOP gate, real money) 🛑
Strict order — **never two live bots on one account** (double grid → they cancel each other's orders):
1. 👤 Laptop: stop the bot (Ctrl-C, clean). This flushes the laptop's latest grid state to its (gitignored) `db/grid_anchor.json`.
2. 👤 Confirm to Claude the laptop bot is down (Claude verifies — no process).
3. 🤖 **Migrate grid continuity (the cutover-critical step).** `strat_id` alone does NOT carry the grid — the ladder lives in the laptop's gitignored `db/grid_anchor.json` (+ SQLite grid-state), which `git clone` does not bring. Without it, the VPS builds a **fresh** grid from market price, and on the first ticker after cutover the engine cancels every reconcile-adopted live order not on that fresh grid's price set (`outside_grid`/`side_mismatch`, `reconciler.py:124-135`) → mass cancel-replace of the real 40/60 order book (incl. deep ballast). So: **(a)** delete the shadow-polluted `db/grid_anchor.json` on the droplet, **(b)** copy the laptop's `db/grid_anchor.json` (same `strat_id`/`grid_step`/`grid_count` — `_load_grid_state` only restores on exact match, `runner.py:643-668`) to the droplet, verify ownership/perms. _(Alternative, only if explicitly chosen: accept a fresh-grid restructure and skip the copy — but then expect a full cancel-replace and verify the order book AFTER the first ticks, per step 5.)_
4. 🤖 Droplet: set `shadow_mode: false`, `supervisorctl start gridbot`. Startup reconcile (0086 fail-closed) injects the live open orders for one tick; with the migrated `grid_anchor.json` the rebuilt grid matches, so adopted orders survive.
5. 🤖 **Verify AFTER the first ticks, not before.** The order-count check passes post-reconcile but *pre-first-tick* — i.e. right before any spurious cancels would fire. Let ≥1–2 ticker events process, THEN verify: orders present (~40 LTC / 60 SOL), prices match a pre-cutover order snapshot (not just the count), positions intact, no `outside_grid`/`side_mismatch` cancel burst in the log.
6. 🤖 `/gridbot-health` over SSH — first live VPS window.

### Phase 7R — Rollback (if VPS-live goes wrong) 🛑
Same never-two-bots ordering, in reverse:
1. 👤/🤖 Droplet: `supervisorctl stop gridbot` (graceful). Confirm the process is down (Claude verifies).
2. 🤖 Copy the droplet's current `db/grid_anchor.json` back to the laptop (so the laptop resumes the latest ladder).
3. 👤 Restart the laptop bot; verify reconcile adopts the grid and the order book is intact.
4. Diagnose the VPS issue offline before re-attempting Phase 7.

### Phase 8 — Finalize
- 👤 Confirm several clean windows in a row (Claude monitors).
- 🤖 Now flip `autostart=true` in `gridbot.conf` (`supervisorctl reread && update`) so the single VPS instance survives reboots. (It was `false` through Phases 4–7 to prevent an accidental second live bot.)
- 🤖 Confirm the liveness watchdog (Phase 4) is armed and its external alert channel works (test-fire it).
- 👤 Never start the laptop bot again (VPS is now the single instance). Retire the laptop launchd sampler.
- Result: 24/7, auto-restart on reboot, no `/tmp` wipe, no travel-network, lower latency.

---

## Open items to confirm live
- Reviewed, versioned source and VPS configuration for the `gridbot-health` artifact. The local skill's `analyze.py` defaults `--log=/tmp/gridbot.log` and Table 5 reads `conf/gridbot_test.yaml` — the deployed artifact must read the VPS log path and the VPS config path, keep `health_state.json` beside itself, and (per Phase 4) handle rotated `gridbot.log.1[.gz]`.
- Strict allowlist for any remote health-check options beyond the no-argument check (until then `/gridbot-health --profit-chart|--since|…` over SSH is silently dropped by the forced `command=`).
- Reviewed live configuration staging process for `/home/USER/gridbot/conf/gridbot.yaml` (staged `shadow_mode: true`; gitignored-or-outside-repo, per Phase 3).
- Whether to migrate the existing `health_state.json` (335 samples) to the droplet or start fresh there.
- **Durable bot status path:** 0082 `status_file_path` defaults to `/tmp/gridbot_status.json`, which a VPS reboot wipes. Point it at a persistent path (e.g. `/home/USER/gridbot/gridbot_status.json`) if any tooling reads it.
- Confirm the Bybit API key's IP allowlist includes the SGP1 droplet IP (Phase 0) before the first live start.
- Prerequisites the plan assumes present on the droplet: `git`, a matching Python for `uv python install` (CI uses 3.12), and a system `python3` for the analyzer.

## Notes
- `health_state.json` moves to the droplet (history preserved) OR starts fresh there — user's choice.
- Account / SOL spot-collateral do not depend on where the bot runs.
- Skill edits are local (gitignored, no branch needed).
- **Nothing is executed without an explicit per-phase "go".** Each irreversible step (cutover) is its own confirmation.
