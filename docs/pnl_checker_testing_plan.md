# pnl_checker Live Testing Plan

> **Symbol: LTCUSDT** — cheapest min order (~$5.29, margin ~$0.53 at 10x), lowest liquidation risk

## Phase 1: Bybit Sub-Account Setup

### Step 1 — Generate API Key on sub-account ✅
- [x] Log into Bybit, switch to your unused sub-account
- [x] Navigate to **API Management** → **Create New Key**
- [x] Select **System-Generated API Keys**
- [x] Set permissions to **read-only** only:
  - [x] Position: Read
  - [x] Account: Read (wallet balance)
  - [x] Exchange History: Read (transaction/funding log)
  - [x] Do NOT enable Order, Trade, or any write permissions
- [x] IP restriction: No restriction
- [x] Save API Key and API Secret securely (secret shown only once)

### Step 2 — Transfer USDT to sub-account ✅
- [x] From main Bybit account → **Assets** → **Transfer**
- [x] Transfer **$15-20 USDT** to sub-account's Unified Trading Account
- [x] Verify balance shows up in sub-account

### Step 3 — Verify sub-account settings ✅
- [x] Confirm sub-account is in **Unified Trading Account (UTA)** mode
- [x] Go to **Derivatives** → **LTCUSDT Perpetual**
- [x] Set **Position Mode** to **Hedge Mode** (Both Side)
- [x] Set **Leverage** to **10x** for both Buy and Sell sides

---

## Phase 2: Open Test Positions

### Step 4 — Open a LONG LTCUSDT position ✅
- [x] In Bybit UI → LTCUSDT Perpetual → **Buy/Long**
- [x] Use **Market order**
- [x] Place minimum size: **0.1 LTC** (~$5.29 notional, ~$0.53 margin at 10x)
- [x] Confirm long position appears in Positions tab

### Step 5 — Open a SHORT LTCUSDT position ✅
- [x] Same pair → **Sell/Short**
- [x] Market order, minimum size: **0.1 LTC**
- [x] Confirm both long AND short positions show in Positions tab
- [x] Note down avg entry prices and unrealized PnL from Bybit UI

> **Margin estimate:** ~$1.06 total for both positions. With $15-20 in the account you have massive buffer against liquidation.

---

## Phase 3: Configure & Run pnl_checker

### Step 6 — Set up environment variables
- [X] Set env vars (add to `~/.zshrc` or local gitignored `.env`):
  ```bash
  export BYBIT_API_KEY="your_api_key_here"
  export BYBIT_API_SECRET="your_api_secret_here"
  ```

### Step 7 — Create config file ✅
- [x] Copy example config:
  ```bash
  cp apps/pnl_checker/conf/pnl_checker.yaml.example apps/pnl_checker/conf/pnl_checker.yaml
  ```
- [x] Edit config:
  - [x] Remove/comment out `api_key` and `api_secret` (using env vars)
  - [x] Set symbols to `LTCUSDT` with `tick_size: "0.01"`
  - [x] Adjust `risk_params.max_margin` to match ~$15-20 budget
  - [x] Keep `tolerance: 0.01` (default)

### Step 8 — Sync dependencies and first run
- [X] Sync workspace:
  ```bash
  cd /Users/erzhan/DATA/PROJ/grid-bot-validation
  uv sync
  ```
- [ ] Run pnl_checker:
  ```bash
  uv run python -m pnl_checker.main --config apps/pnl_checker/conf/pnl_checker.yaml
  ```

### Step 9 — Inspect first run results
- [ ] Both LONG and SHORT LTCUSDT positions detected
- [ ] All checked fields showing **PASS**
- [ ] Cross-reference Unrealized PnL (mark) with Bybit UI
- [ ] Check JSON output in `output/pnl_check_*.json`

---

## Phase 4: Continuous Monitoring

### Step 10 — Run multiple times as price moves
- [ ] Run periodically (every 5 min) over 30-60 minutes:
  ```bash
  while true; do
    echo "=== $(date) ==="
    uv run python -m pnl_checker.main --config apps/pnl_checker/conf/pnl_checker.yaml
    echo ""
    sleep 300
  done
  ```

### Step 11 — Validate consistency
- [ ] All checks PASS consistently across multiple runs
- [ ] Delta values stay at or near 0 as LTC price moves
- [ ] JSON files accumulating in `output/` for audit trail
- [ ] No unexpected FAILs

---

## Phase 5: Edge Case Validation (Optional)

### Step 12 — Test with --debug flag
- [ ] Run with debug logging:
  ```bash
  uv run python -m pnl_checker.main --config apps/pnl_checker/conf/pnl_checker.yaml --debug
  ```
- [ ] Review full API responses and calculation details

### Step 13 — Test with tighter tolerance
- [ ] Run with $0.001 tolerance:
  ```bash
  uv run python -m pnl_checker.main --tolerance 0.001
  ```
- [ ] Note if calculations hold at tighter precision

---

## Settings Summary

| Setting | Choice |
|---------|--------|
| Sub-account | Existing unused, UTA mode |
| API key | Read-only, no IP restriction |
| Symbol | LTCUSDT (min order 0.1 LTC, ~$5.29) |
| Positions | Both long + short (hedge mode) |
| Leverage | 10x |
| Budget | ~$15-20 USDT |
| Margin needed | ~$1.06 total (both positions) |
| Credentials | Environment variables only |
| Validation | Continuous monitoring, consistent PASS |
| After testing | Keep positions running |

## Min Order Reference

| Symbol | Min Qty | ~USD Value | ~Margin (10x) |
|--------|---------|------------|----------------|
| LTCUSDT | 0.1 LTC | ~$5.29 | ~$0.53 |
| BCHUSDT | 0.01 BCH | ~$5.37 | ~$0.54 |
| SOLUSDT | 0.1 SOL | ~$8.09 | ~$0.81 |
| ETHUSDT | 0.01 ETH | ~$19.30 | ~$1.93 |
| BTCUSDT | 0.001 BTC | ~$66.31 | ~$6.63 |
