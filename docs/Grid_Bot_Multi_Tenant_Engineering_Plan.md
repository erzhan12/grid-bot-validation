# Grid Bot Validation System

## Comprehensive Multi-Tenant Engineering Plan

**Shared Strategy Core + Event-Driven Architecture + Multi-User Support**

Bybit Derivatives | Cross Margin | Limit Orders Only

*December 2025*

---

## 0. Codebase Audit

This section provides a detailed analysis of all three repositories, identifying architecture, strategy logic locations, code duplication, and blockers for multi-tenant support.

### 0.1 Live Bot Repository (bbu2-master)

#### Architecture Summary

The live bot uses a polling-based architecture with WebSocket data feeds and REST order management:

- **main.py** → Entry point, instantiates Controller and runs check_job() loop
- **controller.py** → Orchestrates strategies and exchange connections; manages order routing
- **strat.py (Strat50)** → Grid trading logic; _check_pair_step() is the main strategy tick
- **greed.py** → Grid level calculation: build_greed(), update_greed(), center_greed()
- **bybit_api_usdt.py** → Exchange adapter with WebSocket handlers and REST methods (570 lines)
- **position.py** → Position tracking with margin calculations and amount multiplier logic
- **settings.py** → YAML-based configuration loading from conf/config.yaml and keys.yaml

#### Strategy Logic Locations

| File | Key Functions | Lines |
|------|---------------|-------|
| strat.py | Strat50._check_pair_step(), _check_and_place(), __place_greed_orders() | 79-182 |
| greed.py | build_greed(), update_greed(), __center_greed(), __is_too_close() | 18-168 |
| position.py | __calc_amount_multiplier(), update_position() | 52-106 |
| bybit_api_usdt.py | new_limit_order(), _is_good_to_place(), check_positions_ratio() | 246-562 |

#### Bybit-Specific Logic Mixed with Strategy (Critical Issue)

The following files mix exchange-specific code with strategy logic, preventing shared core usage:

- **greed.py:26-40** → Uses BybitApiUsdt.round_price() directly for price rounding
- **strat.py:49-51** → init_symbol() calls bm.connect_ws_public(), bm.connect_ws_private() directly
- **strat.py:190-194** → get_last_close() fetches via controller→bm chain (network call in strategy)
- **position.py:52-92** → Amount multiplier logic depends on live position data format

#### Multi-Tenant Blockers

- **Single-account design:** Settings.bm_keys loaded globally at startup (settings.py:63)
- **Plaintext secrets:** API keys stored in YAML files (conf/keys.yaml referenced in settings.py:14)
- **No session tracking:** No run_id or user_id to group related trades
- **Global state:** BybitApiUsdt.ticksizes is a class-level dict shared across instances
- **No fault isolation:** Single exception in controller.check_job() affects all accounts

### 0.2 History Saver Repository (trad_save_history-main)

#### Architecture Summary

Well-structured data collection service using pybit WebSocket and SQLAlchemy ORM:

- **main.py** → Entry point, initializes BybitWebSocketClient
- **services/websocket_client.py** → WebSocket connection with ticker_stream subscription only
- **services/data_processor.py** → Queue-based async database writes with bulk_save_objects()
- **models/market_data.py** → TickerData SQLAlchemy model with 20+ fields

#### Critical Gap: Ticker-Only Data

**MAJOR ISSUE:** The current saver only records ticker data filtered by lastPrice changes (websocket_client.py:49-63). This is insufficient for:

- Public trade data needed for trade-through fill simulation
- Private order/execution data needed as ground truth
- Position and wallet snapshots for state reconstruction

#### Current Database Schema

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| ticker_data | id, timestamp, symbol, last_price, bid1_price, ask1_price, funding_rate | Ticker snapshots (only when lastPrice changes) |

### 0.3 Backtest Engine Repository (bbu_backtest-main)

#### Architecture Summary

Event-replay backtester that iterates through ticker_data table. Has sophisticated order management but duplicates live bot logic:

- **src/backtest_engine.py** → Orchestrates backtest, manages funding payments
- **src/backtest_order_manager.py** → Simulated order fills with simple price-crossing logic
- **src/strat.py** → DUPLICATED Strat50 with modified _check_pair_step() for replay (431 lines)
- **src/greed.py** → DUPLICATED Greed class (180 lines, ~95% similar)
- **src/bybit_api_usdt.py** → DUPLICATED with backtest mode additions (800+ lines)
- **src/data_provider.py** → Database cursor iteration over ticker_data

#### Code Duplication Analysis

| Live File | Backtest File | Dup % | Key Differences |
|-----------|---------------|-------|-----------------|
| greed.py (168) | src/greed.py (180) | ~95% | Added center_grid() public, removed db persistence |
| strat.py (202) | src/strat.py (431) | ~60% | Added DataProvider iteration, position snapshots |
| position.py (159) | src/position.py (256) | ~70% | Added PositionStatus dataclass, tracker integration |
| bybit_api_usdt (570) | src/bybit_api_usdt (800+) | ~50% | Added backtest mode, simulated fills |

#### Current Backtest Fill Logic Problem

The fill model in backtest_order_manager.py:209-225 uses simple price crossing:

```python
def _should_fill_with_slippage(order, current_price):
    if order.side == BUY: return current_price <= order.limit_price
    else: return current_price >= order.limit_price
```

**Problem:** This optimistic fill model ignores queue position and assumes fills at limit price whenever price crosses. This leads to overly optimistic backtest results.

---

## 1. Target Architecture (Multi-Tenant + Rewrite-for-Compatibility)

### 1.1 Design Principles

1. **Pure Deterministic Strategy Core:** Strategy logic MUST NOT make network calls or have side effects
2. **Event → Intent Pattern:** Strategy consumes normalized events, emits order intents
3. **Adapter Isolation:** All exchange-specific code in adapters (Bybit live vs simulated broker)
4. **Multi-Tenant by Design:** Per-user, per-account workers with fault isolation
5. **Deterministic Ordering:** Events sorted by exchange timestamp, with local timestamp as tiebreaker

### 1.2 Proposed Monorepo Structure

```
grid-bot-validation/
├── pyproject.toml              # UV workspace configuration
├── packages/
│   ├── gridcore/               # Pure strategy core (shared)
│   │   └── src/gridcore/
│   │       ├── events.py       # Normalized event models
│   │       ├── intents.py      # Order intent models
│   │       ├── engine.py       # GridEngine (per-symbol)
│   │       ├── grid.py         # Grid level calculations
│   │       ├── position.py     # Position state tracking
│   │       └── config.py       # GridConfig dataclass
│   └── bybit_adapter/          # Bybit-specific helpers
│       └── src/bybit_adapter/
│           ├── normalizer.py   # WS message → Event
│           ├── executor.py     # Intent → REST API calls
│           └── ws_client.py    # WebSocket management
├── apps/
│   ├── live_bot/               # Multi-tenant live trading
│   │   └── src/
│   │       ├── orchestrator.py # Per-account worker manager
│   │       ├── account_worker.py
│   │       └── data_capture.py # Ground truth logging
│   ├── backtest/               # Backtest application
│   │   └── src/
│   │       ├── replay_feed.py  # Event stream from DB
│   │       ├── sim_broker.py   # Fill simulation
│   │       └── fill_models.py  # Trade-through logic
│   └── event_saver/            # Data collection service
│       └── src/
│           ├── collectors/     # Public + private WS
│           └── reconciler.py   # REST gap filling
└── shared/
    └── db/                     # Shared database models
```

### 1.3 Multi-Tenant Runtime Design

#### Per-Account Worker Model

Each account runs as an independent worker with its own:

- WebSocket connections (public + private)
- GridEngine instances (one per symbol)
- Rate limit tracker
- Error counter for circuit breaker

#### Fault Isolation Strategy

- **Process-level isolation:** Each account worker runs in separate asyncio task with exception boundary
- **Circuit breaker pattern:** After 3 consecutive failures, worker enters backoff mode (exponential: 30s, 60s, 120s)
- **Health monitoring:** Orchestrator tracks worker status, can disable unhealthy accounts
- **Graceful degradation:** One account failure doesn't cascade to others

---

## 2. Multi-Tenant Data Model

### 2.1 Core Entity Tables

#### Users Table

```sql
CREATE TABLE users (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(100) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE,
    status VARCHAR(20) DEFAULT 'active', -- active, suspended, deleted
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### Bybit Accounts Table

```sql
CREATE TABLE bybit_accounts (
    account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(user_id) ON DELETE CASCADE,
    account_name VARCHAR(100) NOT NULL,
    environment VARCHAR(10) NOT NULL, -- 'mainnet' or 'testnet'
    status VARCHAR(20) DEFAULT 'enabled', -- enabled, disabled, error
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, account_name)
);
```

#### API Credentials Table (Encrypted)

```sql
CREATE TABLE api_credentials (
    credential_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES bybit_accounts(account_id) ON DELETE CASCADE,
    api_key_id VARCHAR(100) NOT NULL,  -- Public key (not encrypted)
    encrypted_secret BYTEA NOT NULL,    -- Encrypted with envelope encryption
    encryption_key_id VARCHAR(100),     -- Reference to key in KMS/env
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    rotated_at TIMESTAMPTZ
);
```

#### Strategies Table

```sql
CREATE TABLE strategies (
    strategy_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES bybit_accounts(account_id) ON DELETE CASCADE,
    strategy_type VARCHAR(50) NOT NULL, -- 'GridStrategy'
    symbol VARCHAR(20) NOT NULL,
    config_json JSONB NOT NULL,  -- greed_count, greed_step, etc.
    is_enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(account_id, symbol)
);
```

#### Runs Table

```sql
CREATE TABLE runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(user_id),
    account_id UUID REFERENCES bybit_accounts(account_id),
    strategy_id UUID REFERENCES strategies(strategy_id),
    run_type VARCHAR(20) NOT NULL, -- 'live', 'backtest', 'shadow'
    gridcore_version VARCHAR(50),
    config_snapshot JSONB,
    start_ts TIMESTAMPTZ DEFAULT NOW(),
    end_ts TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'running'
);
```

### 2.2 Event Data Tables (Partitioned)

#### Public Trades (For Trade-Through Fill Model)

```sql
CREATE TABLE public_trades (
    id BIGSERIAL,
    symbol VARCHAR(20) NOT NULL,
    trade_id VARCHAR(50) NOT NULL,
    exchange_ts TIMESTAMPTZ NOT NULL,
    local_ts TIMESTAMPTZ NOT NULL,
    side VARCHAR(4) NOT NULL,
    price DECIMAL(20, 8) NOT NULL,
    size DECIMAL(20, 8) NOT NULL,
    PRIMARY KEY (symbol, exchange_ts, id)
) PARTITION BY RANGE (exchange_ts);
```

#### Private Executions (Ground Truth)

```sql
CREATE TABLE private_executions (
    id BIGSERIAL,
    run_id UUID NOT NULL,
    account_id UUID NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    exec_id VARCHAR(50) NOT NULL,
    order_id VARCHAR(50) NOT NULL,
    order_link_id VARCHAR(50),  -- Client order ID for matching
    exchange_ts TIMESTAMPTZ NOT NULL,
    side VARCHAR(4) NOT NULL,
    exec_price DECIMAL(20, 8) NOT NULL,
    exec_qty DECIMAL(20, 8) NOT NULL,
    exec_fee DECIMAL(20, 8),
    closed_pnl DECIMAL(20, 8),
    raw_json JSONB,
    PRIMARY KEY (account_id, exchange_ts, id)
) PARTITION BY RANGE (exchange_ts);
```

---

## 3. Secrets Management

### 3.1 Encryption Architecture

Use envelope encryption with a two-tier key hierarchy:

- **Master Key (KEK):** Stored in environment variable or KMS; never in database
- **Data Encryption Key (DEK):** Unique per credential; stored encrypted alongside data

#### Implementation

```python
# secrets_manager.py
from cryptography.fernet import Fernet
import os

class SecretsManager:
    def __init__(self):
        self.master_key = os.environ['GRIDBOT_MASTER_KEY']
        self.fernet = Fernet(self.master_key)

    def encrypt_secret(self, plaintext: str) -> bytes:
        return self.fernet.encrypt(plaintext.encode())

    def decrypt_secret(self, ciphertext: bytes) -> str:
        return self.fernet.decrypt(ciphertext).decode()
```

### 3.2 Key Rotation Workflow

1. User adds new API credential via CLI/API
2. System encrypts secret with current master key
3. New credential marked active; old credential marked inactive
4. Worker reloads credentials on next check (or immediate signal)
5. Old credentials can be deleted after grace period

### 3.3 Log Sanitization

Implement structured logging with automatic secret redaction:

```python
class SecretRedactingFilter(logging.Filter):
    PATTERNS = [r'api_secret["\']?\s*[:=]\s*["\'][^"\']+',
                r'secret["\']?\s*[:=]\s*["\'][^"\']+']
    
    def filter(self, record):
        for pattern in self.PATTERNS:
            record.msg = re.sub(pattern, '[REDACTED]', record.msg)
        return True
```

---

## 4. Core Contracts (Python-Level API)

### 4.1 Normalized Event Models

All events tagged with multi-tenant identifiers for isolation and replay:

```python
@dataclass(frozen=True)
class Event:
    event_type: EventType
    symbol: str
    exchange_ts: datetime  # Primary sort key
    local_ts: datetime     # Tiebreaker
    # Multi-tenant tags
    user_id: Optional[UUID] = None
    account_id: Optional[UUID] = None
    run_id: Optional[UUID] = None
```

#### Event Types

| Event | Key Fields | Bybit Topic |
|-------|------------|-------------|
| TickerEvent | last_price, mark_price, bid1, ask1, funding_rate | tickers.{symbol} |
| PublicTradeEvent | trade_id, side, price, size | publicTrade.{symbol} |
| ExecutionEvent | exec_id, order_id, price, qty, fee, pnl | execution (private) |
| OrderUpdateEvent | order_id, status, price, qty, leaves_qty | order (private) |

### 4.2 Order Intent Models

```python
@dataclass(frozen=True)
class PlaceLimitIntent:
    symbol: str
    side: str            # 'Buy' or 'Sell'
    price: Decimal
    qty: Decimal
    reduce_only: bool
    client_order_id: str  # Auto-generated for matching
    grid_level: int       # For comparison reports
```

### 4.3 Strategy Engine API

```python
class GridEngine:
    """Pure strategy engine - NO network calls, NO side effects"""
    
    def __init__(self, config: GridConfig, tick_size: Decimal):
        self.config = config
        self.tick_size = tick_size
        self.grid = Grid(config, tick_size)
    
    def on_event(self, event: Event) -> list[Intent]:
        """Process event and return list of intents. PURE FUNCTION."""
        # Strategy logic here - same code runs in live and backtest
```

---

## 5. Data Capture Upgrade (Mandatory)

### 5.1 Required WebSocket Streams

#### Public Streams (Market Data)

| Stream | Bybit Topic | Purpose | Rate |
|--------|-------------|---------|------|
| Ticker | tickers.{symbol} | BBO, mark price, funding | ~100ms |
| **Public Trades** | publicTrade.{symbol} | **MANDATORY: Fill simulation** | Every trade |

#### Private Streams (Per Account - Ground Truth)

| Stream | Topic | Purpose | When |
|--------|-------|---------|------|
| Orders | order | Order lifecycle tracking | On change |
| **Executions** | execution | **GROUND TRUTH for validation** | On fill |
| Positions | position | Position state verification | On change |
| Wallet | wallet | Balance reconciliation | On change |

### 5.2 Per-Account Rate Limit Handling

Bybit rate limits apply per API key. Each account worker tracks:

- **Order submission rate:** 10 requests/second per category
- **Query rate:** 20 requests/second
- **Backoff strategy:** On 429 response, exponential backoff (1s, 2s, 4s)

---

## 6. Backtest Design (Event Replay, No Orderbook)

### 6.1 Trade-Through Fill Model

Instead of simulating orderbook queue position (which requires L2 data), we use public trades:

| Fill Mode | Logic | Use Case |
|-----------|-------|----------|
| OPTIMISTIC | Fill when price crosses limit | Current behavior; best-case scenario |
| **CONSERVATIVE** | Fill when public trade at/through limit | **RECOMMENDED default** |
| PESSIMISTIC | Fill when cumulative volume > order size | Stress testing; worst-case |

#### Fill Model Implementation

```python
class TradeThoughFillModel:
    def check_fill(self, order: PendingOrder, trade: PublicTradeEvent):
        # Check if trade crosses our limit
        if order.side == 'Buy':
            crosses = trade.price <= order.price
        else:
            crosses = trade.price >= order.price
        
        if not crosses: return None
        
        if self.mode == FillMode.CONSERVATIVE:
            return order.qty - order.filled_qty  # Full fill
```

### 6.2 Replay Feed Design

ReplayFeed merges multiple tables into single sorted stream by run_id and account_id:

```python
class ReplayFeed:
    def __init__(self, run_id: UUID, account_id: UUID):
        self.run_id = run_id
        self.account_id = account_id
    
    def iterate(self, start: datetime, end: datetime) -> Iterator[Event]:
        """Yield events in deterministic order using heap merge"""
```

---

## 7. Comparison and Validation

### 7.1 Validation Workflow

1. **Run Live Bot:** Execute with data capture enabled, generating run_id
2. **Run Backtest:** Load run config, replay same time window with same gridcore version
3. **Compare:** Match executions by client_order_id (orderLinkId) and grid_level
4. **Report:** Generate metrics and identify divergences

### 7.2 Comparison Metrics

| Metric | Description | Target |
|--------|-------------|--------|
| Trade Count Match | live_trades / sim_trades ratio | > 0.95 |
| Price Slippage | avg(\|live_price - sim_price\|) / tick_size | < 2 ticks |
| Timing Drift | avg(\|live_ts - sim_ts\|) | < 1 second |
| P&L Delta | \|live_pnl - sim_pnl\| / live_pnl | < 5% |
| Missed Trades | Trades in live but not in sim | < 5% |

### 7.3 Multi-Tenant Access Controls

Comparison reports are scoped by user_id:

- User can only access runs where run.user_id matches their user_id
- CLI accepts --user-id parameter for filtering
- Database queries always include user_id in WHERE clause

---

## 8. Implementation Roadmap

### 8.1 Phase Overview

| # | Phase | Deliverables | Effort | Priority |
|---|-------|--------------|--------|----------|
| A | Repo Setup | Monorepo, UV workspace, shared deps | 2-3 days | **Critical** |
| B | Core Library | gridcore: events, intents, engine, grid | 5-7 days | **Critical** |
| C | Multi-Tenant DB | Users, accounts, credentials, strategies | 3-4 days | **Critical** |
| D | Data Capture | Public trades + private streams | 4-5 days | **Critical** |
| E | Live Bot Rewrite | Multi-tenant orchestrator, workers | 5-7 days | High |
| F | Backtest Rewrite | Trade-through fill, replay feed | 5-6 days | High |
| G | Comparator | Matching algorithm, metrics, reports | 3-4 days | Medium |
| H | Testing | Integration tests, shadow-mode validation | 5-7 days | Medium |

**Total Estimated:** 33-43 days

### 8.2 Phase B Details: Core Library Extraction

#### Tasks

1. Extract grid.py from bbu2/greed.py, parameterize tick_size (remove BybitApiUsdt.round_price)
2. Extract engine.py from bbu2/strat.py:Strat50._check_pair_step(), convert to on_event()
3. Define events.py with frozen dataclasses (TickerEvent, PublicTradeEvent, ExecutionEvent)
4. Define intents.py with PlaceLimitIntent, CancelIntent
5. Write unit tests for grid calculations (100% coverage)

#### Files Touched

- bbu2/greed.py → packages/gridcore/src/gridcore/grid.py
- bbu2/strat.py → packages/gridcore/src/gridcore/engine.py
- NEW: packages/gridcore/src/gridcore/events.py, intents.py, config.py

#### Done Criteria

- gridcore has zero imports from pybit or any exchange-specific code
- Grid calculations produce identical results to original greed.py
- All unit tests pass with pytest

---

## 9. Testing Strategy

### 9.1 Test Categories

#### Core Unit Tests

- Grid calculations: build_greed(), center_grid(), price rounding
- Engine state transitions: on_event() produces correct intents
- Event ordering: deterministic sorting by timestamps

#### Multi-Tenant Security Tests

- Secrets never appear in logs (grep test on log output)
- User A cannot access User B's runs/data
- Encrypted secrets can be decrypted only with correct master key

#### Integration Tests

- Event saver captures all required streams
- Backtest replay produces consistent results across runs
- Comparison matches trades by client_order_id correctly

#### Load/Concurrency Tests

- 10 accounts, 4 symbols each: all workers run without deadlock
- One account failure doesn't cascade to others
- Rate limits respected per account

---

## 10. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Bybit API changes | Pin pybit version; abstract through bybit_adapter; monitor Bybit changelog |
| WebSocket disconnects | Implement reconnect with REST reconciliation; gap detection + resync |
| Database growth | Partition by month; implement retention policy; archive old runs |
| Fill model accuracy | Run shadow-mode with all three fill modes; compare against live fills |
| Code drift (live vs backtest) | CI/CD checks that both import same gridcore version |
| Multi-tenant data leakage | Always filter by user_id in queries; row-level security in PostgreSQL |
| Secret exposure | Log sanitization filter; never log raw credential objects; audit logging |

---

## 11. Critical Questions and Assumptions

### 11.1 Assumptions Made

1. Bybit API v5 remains stable for linear perpetuals
2. Public trade data provides sufficient information for realistic fill simulation
3. PostgreSQL is the target database (partitioning syntax assumes PG 11+)
4. Master encryption key management via environment variables is acceptable for initial deployment
5. 10-20 concurrent accounts is the initial scale target

### 11.2 Biggest Risks from "No Orderbook" Approach

- **Queue position uncertainty:** Without L2 data, we cannot model where our order sits in the queue. Conservative fill mode partially mitigates this.
- **Partial fills:** Current model assumes full fills; real trading may have partials. Consider adding partial fill mode in Phase F.

### 11.3 Quickest MVP to Validate

**Recommended minimal validation path:**

1. Extract gridcore with grid.py only (1-2 days)
2. Add public trades capture to existing saver (1 day)
3. Run shadow-mode: live bot + gridcore in parallel, compare intents (2-3 days)
4. Measure: intent count match, price match, timing match

*This MVP validates the core extraction without full multi-tenant complexity.*

---

*— End of Engineering Plan —*
