---
paths:
  - "packages/bybit_adapter/**"
---

## bybit_adapter — Exchange Interface

**Path**: `packages/bybit_adapter/` | **Dependencies**: `pybit>=5.8`, `gridcore`

### Components

- `normalizer.py` — Converts Bybit WebSocket messages to gridcore events
- `ws_client.py` — Public/Private WebSocket clients with heartbeat watchdog
- `rest_client.py` — REST API with rate limiting
- `rate_limiter.py` — Sliding window with exponential backoff

### Event Normalization

| Source | Target | Key Fields |
|--------|--------|------------|
| `publicTrade.{symbol}` | `PublicTradeEvent` | trade_id, exchange_ts, side, price, size |
| `execution` | `ExecutionEvent` | exec_id, order_id, order_link_id, price, qty, fee, closed_pnl |

Filters: `category=="linear"`, `execType=="Trade"`, `orderType=="Limit"`

### Key Rules

- Import as `from bybit_adapter.normalizer import BybitNormalizer` (not `Normalizer`)
- `BybitRestClient` requires `api_key` and `api_secret` (even if empty for public endpoints)
- REST methods are synchronous `def` (not async) — wrap with `asyncio.to_thread()` in async code
- `get_executions()` returns `tuple[list, cursor]`
- **Execution `category` lives in different places per transport (feature 0110, audit #270 F4)**: WS `execution` rows carry `category` per row (normalizer filters it). REST `/v5/execution/list` puts `category` on the `result` ENVELOPE only — rows have none. `get_executions` validates `result.category == "linear"` on every NON-EMPTY page and raises `ValueError` otherwise (propagates through `get_executions_all`); an empty page is accepted regardless (Bybit's empty-page shape is unverified). Never filter REST rows on a per-row `category` — that silently dropped every recovered execution before 0110.
- **Execution PnL is tri-state (feature 0110)**: `ExecutionEvent.closed_pnl` is `Optional[Decimal]`, default `None` (UNKNOWN). `normalizer.parse_exec_pnl(row)` is the single parser for WS and REST rows: `closedPnl` then `execPnl`; present → `Decimal(str(x))`, missing/`None`/`""` → `None`; an explicit `"0"` stays a known zero. REST `/v5/execution/list` rows carry NEITHER field. Bybit `execPnl` = PnL of a close (the transaction-log `cashFlow`, gross of fees). `normalize_execution` logs a WARNING (`exec_id`, symbol, `closedSize`) when a WS Trade row has no PnL — Bybit documents `execPnl` on WS rows, so that line is a payload surprise, and the row then makes live-check SKIP its window.
- WebSocket handlers run on pybit's thread — use `asyncio.run_coroutine_threadsafe()` not `asyncio.create_task()`

### Bybit V5 API Status

Valid: `New`, `PartiallyFilled`, `Filled`, `Cancelled`, `Rejected`, `Untriggered`, `Triggered`, `Deactivated`

**`Active` is V3 legacy** — bbu2 checked it but V5 never returns it. gridcore only checks V5 statuses.

---

