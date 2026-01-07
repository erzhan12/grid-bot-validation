# Live Bot Running and Debugging Guide

## Current State

**Important**: The current project has extracted the core strategy logic (`gridcore` package) but **has not yet implemented the new live bot** (Phase E: Live Bot Rewrite). 

For now, you can run and debug the **reference live bot** located in `bbu_reference/bbu2-master/`. This reference bot uses the old architecture but is fully functional for testing and debugging.

---

## Running the Reference Live Bot

### Prerequisites

1. **Install dependencies** in the reference bot directory:
```bash
cd bbu_reference/bbu2-master
uv sync
```

2. **Configure API keys** - Create `conf/keys.yaml`:
```yaml
bm_keys:
  - name: your_account_name
    key: YOUR_API_KEY
    secret: YOUR_API_SECRET
    exchange: bybit_usdt
    strat: 1  # Must match id in config.yaml

default_key:
  key: YOUR_API_KEY
  secret: YOUR_API_SECRET

telegram:
  bot_token: ""  # Optional
  chat_id: ""    # Optional
```

3. **Configure strategy** - Edit `conf/config.yaml`:
   - Set `pair_timeframes` for your trading pairs
   - Set `amounts` to match your account names
   - Adjust `greed_count`, `greed_step`, `max_margin`, etc.

4. **Enable debug mode** - Edit `conf/server_config.yaml`:
```yaml
debug: True  # Set to True for verbose logging
```

### Running the Bot

```bash
cd bbu_reference/bbu2-master
uv run python main.py
```

The bot will:
- Initialize WebSocket connections to Bybit
- Start polling loop (check interval: 0.1 seconds by default)
- Log to `logs/YYYY/MM/DD/` directory

---

## Debugging the Live Bot

### 1. Log Files

The bot creates daily log files in `logs/YYYY/MM/DD/`:

- **`log.log`** - General application log (INFO level)
- **`check.log`** - Strategy check loop messages
- **`orders.log`** - Order placement/cancellation events
- **`exceptions.log`** - Exception traces and errors

**Monitor logs in real-time:**
```bash
# Watch general log
tail -f logs/$(date +%Y/%m/%d)/log.log

# Watch order events
tail -f logs/$(date +%Y/%m/%d)/orders.log

# Watch exceptions
tail -f logs/$(date +%Y/%m/%d)/exceptions.log
```

### 2. Debug Mode

Enable debug mode in `conf/server_config.yaml`:
```yaml
debug: True
```

This enables:
- More verbose logging
- Additional validation checks
- Error price threshold checks (`ERROR_PRICE = 0.13`)

### 3. Python Debugger (pdb)

Add breakpoints in the code:

```python
# In controller.py
def check_job(self):
    while True:
        import pdb; pdb.set_trace()  # Breakpoint here
        try:
            Loggers.check_new_day()
            self.__check_step()
            time.sleep(Settings.INTERVALS['CHECK'])
        except Exception as e:
            Loggers.log_exception(f'{type(e)}: {e}')
```

Run with:
```bash
uv run python main.py
```

### 4. VS Code Debugging

Create `.vscode/launch.json` in `bbu_reference/bbu2-master/`:

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Debug Live Bot",
            "type": "debugpy",
            "request": "launch",
            "program": "${workspaceFolder}/bbu_reference/bbu2-master/main.py",
            "console": "integratedTerminal",
            "cwd": "${workspaceFolder}/bbu_reference/bbu2-master",
            "env": {
                "PYTHONPATH": "${workspaceFolder}/bbu_reference/bbu2-master"
            }
        }
    ]
}
```

Then set breakpoints and press F5 to start debugging.

### 5. Key Debugging Points

**Controller Loop** (`controller.py:58-69`):
- Main event loop that calls `__check_step()` every `Settings.INTERVALS['CHECK']` seconds
- Catches exceptions and logs them

**Strategy Check** (`controller.py:71-78`):
- Calls `strat.check_pair()` for each strategy
- This triggers `Strat50._check_pair_step()` which processes ticker events

**WebSocket Handlers** (`bybit_api_usdt.py`):
- `handle_ticker()` - Receives market price updates
- `handle_execution()` - Receives order fill notifications
- `handle_order()` - Receives order status updates

**Order Placement** (`strat.py:__place_greed_orders()`):
- Builds grid orders and calls `controller.new_order()`
- Check `bybit_api_usdt.py:new_limit_order()` for actual API calls

### 6. Common Issues to Debug

**No orders being placed:**
- Check `check.log` for strategy check messages
- Verify WebSocket connection in `log.log`
- Check if grid is built (should see "Building grid from market price" message)
- Verify API keys are correct and have trading permissions

**Orders not filling:**
- Check `orders.log` for placed orders
- Verify order prices are within market range
- Check position limits in `position.py`
- Review `exceptions.log` for API errors

**WebSocket disconnections:**
- Check `exceptions.log` for connection errors
- Verify network connectivity
- Check Bybit API status
- Review rate limiting (429 errors)

**Position management issues:**
- Check `position.py:__calc_amount_multiplier()` logic
- Review liquidation risk calculations
- Verify margin levels from exchange

---

## Architecture Overview (Reference Bot)

```
main.py
  └─> Controller.__init__()
        ├─> Settings.read_settings()  # Load config.yaml, keys.yaml
        ├─> Loggers.init_loggers()    # Setup logging
        ├─> __init_bms()              # Initialize BybitApiUsdt instances
        └─> __init_strats()           # Initialize Strat50 instances
  └─> Controller.check_job()
        └─> while True:
              ├─> __check_step()
              │     └─> strat.check_pair()  # For each strategy
              │           └─> Strat50._check_pair_step()
              │                 ├─> Get ticker from WebSocket
              │                 ├─> Build/update grid
              │                 └─> Place/cancel orders
              └─> time.sleep(Settings.INTERVALS['CHECK'])
```

**Key Components:**
- **Controller** - Orchestrates strategies and exchange connections
- **Strat50** - Grid trading strategy (old version, not using gridcore yet)
- **BybitApiUsdt** - Exchange adapter with WebSocket + REST API
- **Position** - Position risk management
- **Grid (greed.py)** - Grid level calculations

---

## Future: New Live Bot (Phase E)

According to `RULES.md`, Phase E will implement:
- **Multi-tenant orchestrator** using `grid_db` package
- **Integration with `gridcore`** package (pure strategy engine)
- **Event-driven architecture** (events → intents → execution)
- **Per-account workers** with rate limit handling
- **Data capture** for validation (public trades + private executions)

The new bot will:
1. Load strategies from database (`strategies` table)
2. Create `GridEngine` instances from `gridcore` package
3. Process WebSocket events → convert to `Event` objects
4. Call `engine.on_event()` → get `Intent` objects
5. Execute intents via exchange API
6. Store execution data for validation

**Until Phase E is implemented**, use the reference bot for live trading and debugging.

---

## Quick Debug Checklist

- [ ] API keys configured in `conf/keys.yaml`
- [ ] Strategy config matches in `conf/config.yaml`
- [ ] Debug mode enabled in `conf/server_config.yaml`
- [ ] Log directory exists and is writable
- [ ] WebSocket connections established (check `log.log`)
- [ ] Grid building successfully (check `check.log`)
- [ ] Orders being placed (check `orders.log`)
- [ ] No exceptions in `exceptions.log`
- [ ] Position data updating correctly
- [ ] Exchange API responding (no 429 rate limit errors)

---

## Testing with Testnet

To avoid real money during debugging:

1. **Use Bybit testnet** - Set `is_testnet: true` in `conf/config.yaml`:
```yaml
amounts:
  - name: test_account
    amount: x0.001
    strat: 1
    is_testnet: true  # Use testnet
```

2. **Get testnet API keys** from https://testnet.bybit.com/

3. **Verify testnet connection** - Check logs for testnet URL usage

---

## Additional Resources

- **Bybit API Docs**: https://bybit-exchange.github.io/docs/v5/
- **Reference Code**: `bbu_reference/bbu2-master/`
- **Core Strategy**: `packages/gridcore/` (new architecture)
- **Database Layer**: `shared/db/` (for future multi-tenant bot)
- **Project Rules**: `RULES.md`

