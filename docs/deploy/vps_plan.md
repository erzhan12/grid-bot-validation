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
- The key would only leak if its file were read/`cat`ed — Claude never reads private keys, `.env`, or secrets.
- `BYBIT_API_KEY` / `BYBIT_API_SECRET` live in `/home/USER/gridbot/.env` (`chmod 600`), read by the bot via env — never printed, never in context.
- **Forced-command** makes the health key useless beyond running the analyzer even if it leaked.

---

## Phases

Legend: 👤 = user does · 🤖 = Claude does (over SSH / locally) · 🛑 = STOP gate (wait for explicit confirmation)

### Phase 0 — Droplet
- 👤 Create new DO droplet: Ubuntu 24.04 LTS, 1 vCPU / 1 GB, SGP1.
- 👤 Give Claude: droplet IP + sudo (non-root) username.
- 👤 Base hardening (can defer): `ufw`, `fail2ban`.

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
- 🤖 Wrapper `run-gridbot.sh` (`chmod 700`):
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  cd /home/USER/gridbot
  set -a; source .env; set +a
  exec uv run python -m gridbot.main \
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
  autostart=true
  autorestart=true
  startsecs=30
  stopsignal=INT
  stopwaitsecs=30
  stdout_logfile=/home/USER/gridbot/console.log
  stderr_logfile=/home/USER/gridbot/console.log
  ```
  - `stopsignal=INT` + `stopwaitsecs=30` → clean Ctrl-C-equivalent graceful shutdown.
  - JSON log (for the analyzer) is written by the bot itself to `gridbot.log`.
- 🤖 `logrotate` `/etc/logrotate.d/gridbot`: `gridbot.log` — `daily`, `rotate 7`, **`copytruncate`** (safe with Python append-mode `FileHandler` — writes always at EOF, no sparse holes), `compress`, `missingok`, `notifempty`.
- 🤖 **Hourly profit sampler** (moved off the laptop launchd agent): after Phase 5 provisions the reviewed VPS health artifact, add `0 * * * * /usr/bin/python3 /home/USER/gridbot-health/analyze.py --log /home/USER/gridbot/gridbot.log --sample-only` → even 1-hour `activity` cadence for `--profit-chart` / `--csv`. On the VPS this is robust (no sleep/reboot). Retire the laptop `com.gridbot.health-sampler` launchd agent at cutover.
- ⚠️ Do NOT start the bot live here — next phase is a shadow dry-run.

### Phase 5 — Forced-command health channel + rewire skill
- 👤 Generate a **second** key `gridbot-health` (Claude's monitoring channel, separate from the deploy key).
- 🛑 **Prerequisite:** `gridbot-health` is a local, gitignored artifact; `git clone` does not provide it. Create or select a reviewed, versioned deployment artifact before copying it to `/home/USER/gridbot-health`. It must not retain the laptop's absolute config path, must read the VPS log path, and must keep `health_state.json` beside the deployed analyzer.
- 🤖 `run-analyze.sh` on the droplet:
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  exec /usr/bin/python3 /home/USER/gridbot-health/analyze.py \
    --log /home/USER/gridbot/gridbot.log
  ```
- 👤 In the droplet's `~/.ssh/authorized_keys`, for the health key:
  ```
  command="/home/USER/gridbot-health/run-analyze.sh",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA...gridbot-health
  ```
  → this key can ONLY run the no-argument health check. Leaked → useless.
- 🤖 If remote analyzer options are required, add a strict allowlist for exact `SSH_ORIGINAL_COMMAND` forms to `run-analyze.sh`; do not use `eval` or pass arbitrary SSH input to a shell. Until then, the local invocation is exactly `ssh gridbot-health-vps` (alias bound to the health key). `health_state.json` lives on the droplet beside `analyze.py`. Tables identical for the user.
- ✅ Check: `/gridbot-health` over SSH returns tables.

### Phase 6 — Shadow dry-run (validate WITHOUT real orders) 🛑
- 🤖 On the droplet `/home/USER/gridbot/conf/gridbot.yaml`: set `shadow_mode: true` for both strategies.
- 🤖 `supervisorctl start gridbot`. Shadow = intents logged, **no real orders placed/cancelled**. WS/reconcile are read-only → **safe to run alongside the live laptop bot**.
- 🤖 Verify over SSH: startup reconcile OK, JSON log writing, `/gridbot-health` works, Bybit latency from Singapore.
- 🛑 STOP — show dry-run results. Cutover only after user OK.

### Phase 7 — Cutover to live (STOP gate, real money) 🛑
Strict order — **never two live bots on one account** (double grid → they cancel each other's orders):
1. 👤 Laptop: stop the bot (Ctrl-C, clean).
2. 👤 Confirm to Claude the laptop bot is down (Claude verifies — no process).
3. 🤖 Droplet: `shadow_mode: false`, `supervisorctl restart gridbot`.
4. 🤖 Startup reconcile (0086 fail-closed) **adopts the live grid** — same `strat_id` → continuity. Verify: reconcile complete, orders present (40 LTC / 60 SOL), positions intact.
5. 🤖 `/gridbot-health` over SSH — first live VPS window.

### Phase 8 — Finalize
- 👤 Confirm several clean windows in a row (Claude monitors).
- 👤 Never start the laptop bot again (VPS is now the single instance).
- Result: 24/7, auto-restart on reboot, no `/tmp` wipe, no travel-network, lower latency.

---

## Open items to confirm live
- Reviewed, versioned source and VPS configuration for the `gridbot-health` artifact.
- Strict allowlist for any remote health-check options beyond the no-argument check.
- Reviewed live configuration staging process for `/home/USER/gridbot/conf/gridbot.yaml`.
- Whether to migrate the existing `health_state.json` (335 samples) to the droplet or start fresh there.

## Notes
- `health_state.json` moves to the droplet (history preserved) OR starts fresh there — user's choice.
- Account / SOL spot-collateral do not depend on where the bot runs.
- Skill edits are local (gitignored, no branch needed).
- **Nothing is executed without an explicit per-phase "go".** Each irreversible step (cutover) is its own confirmation.
