# 0029 — Phase 4 operational runbook (comparator parity smoke)

Operational instructions for running seed-aware replay end-to-end and
verifying parity through the comparator. Phase 4 is the validation
step deferred from the 0029 PR (#68); code is on `main` from
`0d7a35f`.

## What we verify

Replay seeded from a recorder DB at `t=start_ts` should produce
intents that are **identical to live** over the same recorded window.
Acceptance metric: `match_rate ≥ 0.95` from `apps/comparator`.

## Architecture

Two processes run in parallel during the recording window:

- **`apps/gridbot`** (live) — trades on Bybit mainnet via WS+REST.
  Writes nothing to DB; recording is a separate process by design.
- **`apps/recorder`** — independent writer. Always subscribes to the
  public **ticker** stream and (when `account` is configured) to the
  private streams (orders, executions, positions, wallet) for the
  same account. Public **trade** firehose is opt-in via the
  `capture_public_trades` flag — when `false` (the default we use
  for Phase 4), the recorder does NOT subscribe to `publicTrade.*`
  on the WS at all (no network frames, no parsing, no write).
  Tables populated for Phase 4: `ticker_snapshots`, `orders`,
  `position_snapshots`, `wallet_snapshots`, `private_executions`.
  Without recorder running, comparator has no ground truth and
  cannot match.

After the window is captured, two offline runs:

- **`apps/replay`** — reads recorder ticker stream + seed-state at
  `t=start_ts` from the same recorder DB → produces its own intents
  and fills via `TradeThroughFillSimulator`.
- **`apps/comparator`** — joins live executions vs replay trades on
  `client_order_id` (with the `client_id = order_link_id or order_id`
  fallback for any pre-cross-cutting-#1 rows).

---

## Prerequisites

- `main` branch checked out at `0d7a35f` or later (the 0029 merge).
- `BYBIT_API_KEY` / `BYBIT_API_SECRET` in `.env` with read permission
  on wallet, position, and orders for `category=linear` /
  `settleCoin=USDT`. Same env vars live gridbot already reads.
- ~30–60 minutes of attended runtime to accumulate ≥30 closed trades
  on LTCUSDT (current grid_step=0.3% on a non-volatile pair).

---

## Step 0 — Stop the existing live (clean-state precondition)

Without a clean restart, seed-mismatch is unavoidable: the recorder
starts at `T0` while live carries accumulated grid + positions from
its prior session.

```bash
# Find the gridbot PID
ps aux | grep gridbot | grep -v grep

# Graceful shutdown — gridbot has a SIGINT handler that flushes state
kill -INT <PID>

# Confirm it's gone
ps aux | grep gridbot | grep -v grep
```

## Step 1 — Cancel open orders; closing positions is OPTIONAL

**Required:** cancel all live limit orders for the symbol via the
Bybit UI or a one-shot REST call. Recorder's initial REST snapshot
will capture them otherwise, replay will seed them, AND a live
walk/recenter immediately after recorder start will diverge replay
from live on tick 1 — the open stack changes faster than seeded
state can model.

**Optional:** closing positions. The seed mechanism handles arbitrary
starting state by design — `_snapshot_positions` writes the real
sizes / entry prices, the loader returns them in `PositionStateSeed`,
the runner mirrors them into `gridcore.Position` via
`_copy_seeded_state_to_positions`. Replay starts in lockstep with
live. Trade-off:

- **Clean baseline (positions closed):** easier to reason about
  parity failures — no position-rules, no early_imbalance, no
  reduce-only complexity. Recommended for the first attempt.
- **Realistic baseline (positions kept):** more representative of a
  real seeded replay; tests the full machinery. Recommended once
  the first run shows `match_rate ≥ 0.95`.

If keeping positions: make sure no NEW orders are placed between
recorder start (Step 4) and `seed.at_ts` until recorder has confirmed
the initial REST snapshot has landed.

## Step 2 — Move aside the restored grid file

```bash
mv db/grid_anchor.json db/grid_anchor.json.bak.$(date +%Y%m%d_%H%M%S)
```

The next gridbot start will build a fresh grid from the first ticker
and `GridStateStore.save` the full level list — no legacy
anchor-only entry will leak into replay.

## Step 3 — Create recorder config for LTCUSDT with private streams

`apps/recorder/conf/recorder_ltcusdt.yaml`:

```yaml
symbols:
  - "LTCUSDT"

database_url: "sqlite:///data/recorder_ltcusdt_phase4.db"
testnet: false

batch_size: 100
flush_interval: 5.0
gap_threshold_seconds: 5.0
health_log_interval: 60   # frequent heartbeat for visibility

# Private streams — REQUIRED for seed-aware replay
account:
  api_key: "${BYBIT_API_KEY}"
  api_secret: "${BYBIT_API_SECRET}"

# When false (default for Phase 4): no `publicTrade.*` WS subscription
# at all — recorder never receives or writes market trades. Ticker
# stream is always subscribed regardless of this flag, and Phase 4
# parity smoke needs only ticker (replay drives off ticker_snapshots).
capture_public_trades: false
```

`${BYBIT_API_KEY}` / `${BYBIT_API_SECRET}` resolve from `.env` via
`dotenv`, the same pattern live gridbot uses.

## Step 4 — Install all workspace packages into the venv (one-time)

Live `gridbot` is in the root `pyproject.toml` dev-deps so it's
already installed; the other apps are not. Install everything at
once so `uv run <app>` works for all of them:

```bash
uv sync --all-packages
```

⚠️ Do NOT use `uv sync --package recorder --package replay --package
comparator` — that flag re-resolves the venv to those three packages
ONLY and uninstalls everything else (gridbot, pytest, pytest-asyncio,
ruff, pnl-checker, etc.), which then breaks live gridbot start and
`uv run pytest`. `--all-packages` keeps the full workspace +
root dev-deps in sync.

Verify:

```bash
uv pip list | grep -iE "recorder|replay|comparator|gridbot|pytest|ruff"
# expect six lines: recorder, replay, comparator, gridbot, pytest, ruff
```

## Step 4b — Start recorder FIRST

Recorder must complete its initial REST snapshot before gridbot
starts placing orders. The seed pre-check requires `MIN(exchange_ts)`
to be non-NULL on `wallet_snapshots` AND `position_snapshots` for the
recorder run.

**Use the helper script** — it handles the failure modes that bare
shell commands have hit in practice (duplicate-recorder PID shadowing,
stale DB rows from prior runs, log file inheritance):

```bash
scripts/phase4/start_recorder.sh
# Optionally: scripts/phase4/start_recorder.sh path/to/other_config.yaml
```

The script: pkills any prior recorder for the same config (15s
graceful), wipes the recorder DB + WAL/SHM + `/tmp/recorder.log`,
starts a fresh recorder in the background, waits up to 15s for the
"Initial REST snapshot" line, and prints PID + next-step commands.

To monitor: `scripts/phase4/status.sh` or `tail -f /tmp/recorder.log`.

**Manual equivalent** (shown for reference; prefer the script):

```bash
uv run recorder \
  --config apps/recorder/conf/recorder_ltcusdt.yaml \
  > /tmp/recorder.log 2>&1 \
  &
RECORDER_PID=$!
echo "Recorder PID: $RECORDER_PID"

# Wait for the initial REST snapshot to land. Use `grep -a` (text mode):
# grep occasionally treats /tmp/recorder.log as binary because the
# bybit-adapter's pybit logger emits non-ASCII frame bytes; -a skips
# the heuristic. Alternatively `tail -f /tmp/recorder.log` works.
sleep 10
grep -a "Initial REST snapshot\|wallet_rows" /tmp/recorder.log
```

**Why not `uv run python apps/recorder/src/recorder/main.py`?** When
Python runs a file by path, only the file's own directory (here
`apps/recorder/src/recorder/`) lands on `sys.path` — its parent (the
directory that makes `recorder` an importable package) does not. The
package's internal imports (`from recorder.config import ...`) then
fail with `ModuleNotFoundError`. After `uv sync --package recorder`
the package is installed in the venv and resolvable from anywhere.

If the log shows `WARNING: Initial REST snapshot incomplete` — stop.
The API key likely lacks read permission on wallet / position /
orders for `category=linear` `settleCoin=USDT`. Fix the credentials
and restart from Step 4.

## Step 5 — Start gridbot

```bash
# Run immediately after the initial-snapshot WARNING-free confirmation
uv run python apps/gridbot/src/gridbot/main.py \
  --config conf/gridbot_test.yaml \
  --log-file /tmp/gridbot.log \
  --debug \
  &
GRIDBOT_PID=$!
echo "Gridbot PID: $GRIDBOT_PID"

# Capture timestamps
START_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
SEED_AT_TS=$(date -u -v+60S +"%Y-%m-%dT%H:%M:%SZ")
echo "START_TS:    $START_TS"
echo "SEED_AT_TS:  $SEED_AT_TS  (start + 60s margin for state to stabilize)"
```

## Step 6 — Accumulate data

Minimum: ≥30 closed trades (≥60 fills) for a statistically meaningful
`match_rate`. On LTCUSDT with grid_step=0.3% this is typically
30–60 minutes.

Live counter check during the run:

```bash
watch -n 30 'sqlite3 data/recorder_ltcusdt_phase4.db "
SELECT
  (SELECT COUNT(*) FROM ticker_snapshots WHERE symbol=\"LTCUSDT\") as tickers,
  (SELECT COUNT(*) FROM private_executions) as executions,
  (SELECT COUNT(*) FROM orders) as order_updates,
  (SELECT COUNT(*) FROM position_snapshots WHERE symbol=\"LTCUSDT\") as positions,
  (SELECT COUNT(*) FROM wallet_snapshots WHERE coin=\"USDT\") as wallets;
"'
```

Also useful: `grep -c "Order placed\|Order filled" /tmp/gridbot.log`.

## Step 7 — Stop both processes; capture identifiers

Capture `END_TS` first (record windows close at this moment):

```bash
END_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "END_TS: $END_TS"
```

Stop gridbot first (so no more orders are placed during the
recorder's drain), then recorder:

```bash
pkill -INT -f "gridbot/main.py --config conf/gridbot_test.yaml"
sleep 5

# Recorder via the helper — prints final counters, RUN_ID, ACCOUNT_ID.
scripts/phase4/stop_recorder.sh
```

The `stop_recorder.sh` output includes the `RUN_ID` and `ACCOUNT_ID`
you'll need to paste into the replay config in Step 8. Save them:

```bash
RUN_ID=$(sqlite3 data/recorder_ltcusdt_phase4.db \
  "SELECT run_id FROM runs ORDER BY start_ts DESC LIMIT 1")
ACCOUNT_ID=$(sqlite3 data/recorder_ltcusdt_phase4.db \
  "SELECT account_id FROM bybit_accounts LIMIT 1")
echo "RUN_ID:     $RUN_ID"
echo "ACCOUNT_ID: $ACCOUNT_ID"
```

Verify nothing's left running:

```bash
ps aux | grep -E "gridbot|recorder" | grep -v grep   # expect: empty
```

Snapshot the final live grid state so replay reads the same file:

```bash
cp db/grid_anchor.json db/grid_anchor.phase4.json
```

## Step 8 — Replay config

The working copy `apps/replay/conf/replay_ltcusdt_phase4.yaml` is
gitignored because `run_id` / timestamps / `account_id` change every
recorder run. Copy from the tracked template and fill the placeholders:

```bash
cp apps/replay/conf/replay_ltcusdt_phase4.yaml.example \
   apps/replay/conf/replay_ltcusdt_phase4.yaml
```

Final content (replace the four `<paste …>` placeholders with the values
captured in Step 7):

```yaml
database_url: "sqlite:///data/recorder_ltcusdt_phase4.db"
run_id: "<paste $RUN_ID>"

symbol: "LTCUSDT"
start_ts: "<paste $SEED_AT_TS>"
end_ts: "<paste $END_TS>"

strategy:
  tick_size: "0.1"
  grid_count: 20
  grid_step: 0.3
  amount: "x0.001"
  commission_rate: "0.0002"
  enable_risk_multipliers: true
  early_imbalance_multiplier: 1.0   # match live default

# Seed block — the 0029 contract
seed:
  enabled: true
  at_ts: "<paste $SEED_AT_TS>"
  account_id: "<paste $ACCOUNT_ID>"
  strat_id: "ltcusdt_test"
  grid_state_path: "db/grid_anchor.phase4.json"
  wallet_coin: "USDT"

fill_simulator:
  mode: book_touch

# Fallback when seed-wallet returns None (shouldn't happen if Step 4
# succeeded with a non-empty wallet on the account)
initial_balance: "10000"

enable_funding: true
funding_rate: "0.0001"
wind_down_mode: "leave_open"

output_dir: "results/replay_ltcusdt_phase4"
price_tolerance: 0
qty_tolerance: "0.001"
```

## Step 9 — Run replay

```bash
uv run python -m replay.main \
  --config apps/replay/conf/replay_ltcusdt_phase4.yaml \
  2>&1 | tee /tmp/replay.log
```

What to look for:

- `Seeded run_id=...: long.size=..., short.size=..., anchor_or_grid_levels=N, balance=..., active_orders=N` — seed wired up successfully.
- `_seed_pre_check` failure with `wallet_min` / `position_min` — recorder's initial REST snapshot didn't land before `seed.at_ts`. Either Step 4 failed silently or `SEED_AT_TS` was set too early.
- `SeedDataQualityError: missing Buy/Sell` — recorder captured exactly one side. Bug in recorder or Bybit returned only the active side without the zero-row pair.

On success, `results/replay_ltcusdt_phase4/`:
- `trades.csv` — replay-side trades (input for comparator).
- `summary.json` — replay metrics.

## Step 10 — Run comparator

```bash
uv run python -m comparator.main \
  --run-id "$RUN_ID" \
  --backtest-trades results/replay_ltcusdt_phase4/trades.csv \
  --start "$SEED_AT_TS" \
  --end "$END_TS" \
  --symbol LTCUSDT \
  --database-url "sqlite:///data/recorder_ltcusdt_phase4.db" \
  --output results/comparison_phase4/ \
  2>&1 | tee /tmp/comparator.log
```

## Step 11 — Interpret results

Inspect the report in `results/comparison_phase4/`. Targets:

| Metric                 | Target          | Acceptable on first run               |
|------------------------|-----------------|----------------------------------------|
| `match_rate`           | ≥ 0.99          | ≥ 0.95                                |
| `price_delta_median`   | 0               | 0 (limit orders = strict cross)       |
| `qty_delta_median`     | 0               | up to 1 `qty_step` (rounding)         |
| `live_only` count      | 0               | explainable per reason                 |
| `backtest_only` count  | 0               | explainable per reason                 |
| `fee_delta`, `pnl_delta` | small         | classify as known-noise                |

**If `match_rate < 0.95`** — read the `live_only` and `backtest_only`
lists in the report:

- `live_only` execution whose `client_order_id` exists as a live
  limit, but replay did not fill → either fill_simulator
  semantics differ from exchange, or seed missed something.
- `backtest_only` order with a `client_order_id` not in live
  executions → engine logic produced an intent live did not.

**If `match_rate ≥ 0.95`** — Phase 4 is closed. Optionally record the
numbers in `docs/features/0029_AUDIT_REPORT.md` for the historical
record.

---

## Common first-run failures

1. **`SeedSchemaError: Order.reduce_only IS NULL`** — the recorder
   wrote orders before the 0029 schema migration was on disk. Check
   the recorder is running from a build at or after `0d7a35f` (the
   0029 merge). If the DB has pre-0029 rows, start a fresh recorder
   DB (`data/recorder_ltcusdt_phase4_v2.db` for the next attempt).
2. **`live_only` executions with `order_link_id IS NULL`** — live
   gridbot is on a build older than cross-cutting #1 (executor sends
   `orderLinkId`). Restart gridbot from a HEAD-or-later build.
3. **`live_only` executions match `backtest_only` 1-to-1 by price/qty
   but client_order_id differs** — a stronger version of #2: live
   placed orders without `orderLinkId` AND comparator's fallback to
   `order_id` doesn't help because replay's `client_order_id` is the
   deterministic SHA-256 hash. Same fix.
4. **Liquidation-driven divergence after the first fill** — known
   noise. Backtest's `_estimate_liquidation_price` overrides the
   seeded exchange `liq_price` on the first `_update_risk_multipliers`
   call. Documented in the plan; classify as known-noise in the
   report.
5. **Wallet seed `None` → fallback to `config.initial_balance`** — if
   the recorder's REST snapshot succeeded but no WS wallet update
   landed before `seed.at_ts`, this branch is taken. Pre-check should
   have caught it; if it didn't, your `seed.at_ts` is earlier than
   the initial REST snapshot timestamp.

---

## Compact checklist

- [ ] Stop live, close positions, cancel orders
- [ ] `mv db/grid_anchor.json db/grid_anchor.json.bak.*`
- [ ] Create `apps/recorder/conf/recorder_ltcusdt.yaml` with private creds
- [ ] Start recorder; wait for `"Initial REST snapshot"` log (no WARNING)
- [ ] Start gridbot; record `START_TS`, compute `SEED_AT_TS = START_TS + 60s`
- [ ] Accumulate ≥30 trades (~30–60 min on LTCUSDT)
- [ ] Stop both; capture `RUN_ID`, `ACCOUNT_ID`, `END_TS`; copy `grid_anchor.json`
- [ ] Create `apps/replay/conf/replay_ltcusdt_phase4.yaml` with seed block
- [ ] Run `replay.main` → check `Seeded run_id=...` log
- [ ] Run `comparator.main` → check `match_rate`
- [ ] If <95% → diagnose `live_only` / `backtest_only`. If ≥95% → done.
