# 0014: `--save-events` Run Plan

## Goal

Validate the embedded EventSaver end-to-end on testnet: confirm that tickers, positions, wallet snapshots, and (for single-strategy accounts) executions and orders are persisted to the database while the gridbot trades normally.

## Prerequisites

- Branch `feat/event-saver-flag` with all review fixes applied
- Bybit testnet API key/secret with Linear Perpetual trading enabled
- Small USDT balance on testnet (10+ USDT is sufficient)

## Step 1: Create Config File

Create `conf/gridbot_test.yaml`:

```yaml
accounts:
  - name: testnet_dev
    api_key: "<BYBIT_TESTNET_API_KEY>"
    api_secret: "<BYBIT_TESTNET_API_SECRET>"
    testnet: true

strategies:
  - strat_id: btcusdt_test
    account: testnet_dev
    symbol: BTCUSDT
    tick_size: "0.1"
    grid_count: 10
    grid_step: 0.3
    amount: "x0.001"
    max_margin: 5.0
    shadow_mode: true  # start with shadow mode to avoid real orders

database_url: "sqlite:///db/gridbot_test.db"

enable_event_saver: true
```

> **Note**: Start with `shadow_mode: true` to validate data capture without placing real orders. Switch to `false` once capture is confirmed.

## Step 2: Initialize Database

The gridbot does not auto-create tables. Run this one-liner first:

```bash
uv run python -c "
from grid_db import DatabaseFactory, DatabaseSettings
db = DatabaseFactory(DatabaseSettings(db_name='db/gridbot_test.db'))
db.create_tables()
print('Tables created')
"
```

Verify:
```bash
sqlite3 db/gridbot_test.db ".tables"
```

Expected tables: `users`, `bybit_accounts`, `strategies`, `runs`, `private_executions`, `orders`, `tickers`, `public_trades`, `position_snapshots`, `wallet_snapshots`, etc.

## Step 3: Start Gridbot with `--save-events`

```bash
uv run python -m gridbot.main \
  --config conf/gridbot_test.yaml \
  --save-events \
  --debug
```

Expected startup logs (in order):
1. `Loaded configuration with 1 strategies`
2. `Initialized account: testnet_dev`
3. `Initialized strategy: btcusdt_test`
4. `Reconciliation complete`
5. `Created Run <uuid> for strategy btcusdt_test`
6. `EventSaver started (symbols=['BTCUSDT'], accounts=1)`
7. `Starting EventSaver...`
8. `EventSaver started. Symbols: ['BTCUSDT'], Accounts: 1`
9. `Connected WebSockets for account: testnet_dev`
10. `Gridbot started successfully`

**Red flags** — stop and investigate if you see:
- `Failed to create Run records` → DB schema issue
- `EventSaver enabled but no accounts configured` → config problem
- `added without run_id` → `_create_run_records()` failed silently

## Step 4: Let It Run (~2-5 minutes)

Watch debug logs for:
- `Ticker: BTCUSDT price=...` — public data flowing
- `Position WS update: testnet_dev/BTCUSDT/...` — position snapshots
- `Wallet: USDT balance=...` — wallet snapshots

## Step 5: Verify Database Contents

Stop the bot with `Ctrl+C`, then query:

```bash
# Check Run record was created and marked completed
sqlite3 db/gridbot_test.db "SELECT run_id, run_type, status, start_ts, end_ts FROM runs;"

# Check ticker data captured
sqlite3 db/gridbot_test.db "SELECT COUNT(*) FROM tickers;"

# Check position snapshots
sqlite3 db/gridbot_test.db "SELECT COUNT(*) FROM position_snapshots;"

# Check wallet snapshots
sqlite3 db/gridbot_test.db "SELECT COUNT(*) FROM wallet_snapshots;"
```

**Expected**:
- 1 Run row with `status='completed'` and non-null `end_ts`
- Ticker count > 0 (should have many after 2+ minutes)
- Position snapshots > 0
- Wallet snapshots > 0

## Step 6: Test with Live Orders (Optional)

Once data capture is confirmed in shadow mode:

1. Change `shadow_mode: false` in config
2. Re-run Step 3
3. Let it place a few grid orders and wait for fills
4. Stop and check:

```bash
# Executions (only persisted when run_id is set — single-strategy accounts)
sqlite3 db/gridbot_test.db "SELECT COUNT(*) FROM private_executions;"

# Orders
sqlite3 db/gridbot_test.db "SELECT COUNT(*) FROM orders;"
```

## Step 7: Compare With/Without `--save-events`

Run the gridbot **without** `--save-events` and confirm:
- No EventSaver logs appear
- No new data is written to capture tables
- Trading behavior is identical

```bash
uv run python -m gridbot.main \
  --config conf/gridbot_test.yaml \
  --debug
```

## Checklist

| # | Check | Pass? |
|---|-------|-------|
| 1 | Bot starts without errors with `--save-events` | |
| 2 | Run record created in `runs` table | |
| 3 | Run record marked `completed` on clean shutdown | |
| 4 | Tickers persisted to `tickers` table | |
| 5 | Position snapshots persisted | |
| 6 | Wallet snapshots persisted | |
| 7 | Executions persisted (live mode, single-strategy) | |
| 8 | Orders persisted (live mode, single-strategy) | |
| 9 | Bot works normally **without** `--save-events` | |
| 10 | No EventSaver activity when flag is off | |

## Known Limitations

- **Multi-strategy accounts**: Executions/orders are captured but NOT persisted (run_id=None). See `docs/features/0014_REVIEW.md` P2 finding.
- **Tables must be pre-created**: `gridbot.main` does not call `db.create_tables()`. This should be done before first run (Step 2).
