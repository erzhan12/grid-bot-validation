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
- WebSocket handlers run on pybit's thread — use `asyncio.run_coroutine_threadsafe()` not `asyncio.create_task()`

### Bybit V5 API Status

Valid: `New`, `PartiallyFilled`, `Filled`, `Cancelled`, `Rejected`, `Untriggered`, `Triggered`, `Deactivated`

**`Active` is V3 legacy** — bbu2 checked it but V5 never returns it. gridcore only checks V5 statuses.

---

