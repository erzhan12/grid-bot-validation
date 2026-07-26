# Bybit Market Data Read API — client protocol

Protocol document version: **1.0**

Last updated: **2026-07-26**

Read-only HTTP API over collected Bybit linear ticker snapshots. The intended
production consumer is another backend service.

- **Production base URL:** provided separately by the API operator; use
  `https://<API_HOST>`.
- **Documented API responses:** successful and handled-error responses use
  `Content-Type: application/json`.
- **Request header:** `Accept: application/json`.
- All endpoints are `GET` and do not accept a request body.

Production clients MUST use HTTPS. Plain HTTP is acceptable only on localhost
or inside a trusted private network/tunnel.

Protocol version `1.0` is the compatibility boundary for the currently
unversioned paths. While a `1.0` consumer remains active, the operator MUST NOT
deploy a breaking change without first publishing a new protocol version and
coordinating the consumer's migration.

Compatible changes include adding an endpoint, adding an optional request
parameter, or adding a response field. Breaking changes include removing or
renaming a field or endpoint; changing a field's type, nullability, meaning, or
required status; changing ordering, time-bound, pagination, authentication, or
error-status semantics. Clients MUST ignore unknown response fields so
compatible fields can be added.

---

## Authentication

When production authentication is enabled, every endpoint except `/health`
requires:

```http
Authorization: Bearer <API_KEY>
```

The API operator provides the key separately through a secure channel. Do not
commit it to source control or include it in URLs, logs, exception messages, or
support screenshots.

This shared-secret scheme is intended for server-to-server use. A key embedded
in browser, desktop, or mobile code cannot be kept secret; such clients require
a separate user-facing authentication design.

Wrong, missing, or malformed credentials return:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json
```

```json
{"detail":"Invalid or missing API key"}
```

The Bearer scheme is case-insensitive. When authentication is enabled,
interactive documentation (`/docs`, `/redoc`, `/openapi.json`) is disabled.

An operator may disable authentication for local development or a protected
private network. Clients must not assume that production endpoints are open.

---

## Timestamps and URL encoding

- Clients SHOULD send query timestamps in UTC with a trailing `Z`, for example
  `2026-07-20T10:15:00Z`.
- The server also accepts ISO-8601 timestamps with an explicit offset and
  converts them to UTC. Naive timestamps are accepted for compatibility and are
  treated as UTC, but clients should avoid them.
- Response timestamps are UTC ISO-8601 strings with a trailing `Z`.
  Microseconds are preserved when present:
  `2026-07-20T10:15:00.123456Z`.
- All documented time bounds are inclusive.

Always construct query strings with the HTTP library's query-parameter encoder.
Do not concatenate raw values. This is particularly important for:

- offsets such as `+05:00`, where `+` must be encoded as `%2B`;
- opaque cursors, which may contain `=` or other characters that require
  encoding.

`timestamp` is the collector's observation time, not Bybit's exchange-event
timestamp. `created_at` is the database insertion time and can be later because
the collector writes rows in batches.

---

## Common conventions

- Symbols are matched exactly. Use a value returned by `/symbols`; do not add
  whitespace or change its case.
- Empty collections return `200` with an empty collection field (`symbols`,
  `items`, or `rows`), not `404`.
- Numeric fields in `TickerRow` and bulk-export rows are JSON numbers backed by
  floating-point storage and may be `null`. They must not be used as exact
  decimal accounting values. Returned kline OHLC fields are non-null.
- `funding_rate` is a fraction, not a displayed percentage:
  `0.0001` means `0.01%`.
- Price, quantity, open-interest, volume, and turnover values retain the units
  supplied by Bybit for the instrument. `volume_24h` is rolling traded quantity;
  `turnover_24h` is rolling traded value. Consumers that convert units should
  use instrument metadata rather than infer units from the JSON field name.
- Clients SHOULD ignore unknown response fields for forward compatibility, but
  must not assume that a documented required field will always be non-null when
  its type explicitly permits `null`.

The market fields originate from Bybit's linear ticker stream. See the
[Bybit ticker field definitions](https://bybit-exchange.github.io/docs/v5/websocket/public/ticker).

### Data availability and freshness

- This API exposes observations that the collector successfully stored. It does
  not guarantee complete coverage of every Bybit update or every time interval.
  Missing rows or candles must not be interpreted as zero trading activity.
- The collector stores change-gated ticker observations. The age of the newest
  `timestamp` is a useful freshness signal, but by itself is not proof that the
  collector is healthy or unhealthy.
- Protocol `1.0` does not define a freshness SLA or minimum history-retention
  period. A production consumer that needs either guarantee must obtain the
  agreed threshold or retention window from the operator as deployment
  configuration.
- `/health` proves only that the HTTP process is alive. Until a separate
  readiness/freshness endpoint is provided, clients should monitor successful
  data queries and apply their configured maximum observation age.
- Historical rows can be removed by operator maintenance or a configured
  retention job. Clients must not rely on indefinite server-side history.

---

## Endpoints

| Method and path | Authentication | Purpose | Order / pagination |
|---|---|---|---|
| `GET /health` | no | HTTP process liveness | — |
| `GET /symbols` | yes | Symbols with stored rows | ascending symbols |
| `GET /ticker/{symbol}/latest` | yes | Newest timestamped row | one row |
| `GET /ticker/{symbol}/history` | yes | Recent rows | descending, limited |
| `GET /ticker_data` | yes | Paginated range export | ascending, cursor |
| `GET /klines` | yes | Derived 1-minute OHLC | ascending minutes |

“yes” means authentication is required when production authentication is
enabled as described above.

### `GET /health` — no authentication

Process liveness check:

```json
{"status":"ok"}
```

This endpoint does not query the database and is not a database-readiness or
data-freshness check. Do not send the API key to `/health`.

---

### `GET /symbols`

Returns the distinct symbols for which stored data exists, sorted ascending:

```json
{"symbols":["BTCUSDT","ETHUSDT","LTCUSDT","SOLUSDT"]}
```

When no symbols are available:

```json
{"symbols":[]}
```

Presence in this list does not guarantee that `/latest` can return a row:
legacy rows for a symbol may all have unusable `null` timestamps.

---

### `TickerRow`

`/ticker/{symbol}/latest` and `/ticker/{symbol}/history` return this row shape.

| Field | JSON type | Nullable | Meaning |
|---|---|---:|---|
| `id` | integer | no | Row identifier; stable while the row remains in the current database |
| `timestamp` | string (date-time) | no | Collector observation time in UTC |
| `symbol` | string | no | Exact Bybit instrument symbol |
| `last_price` | number | yes | Latest traded price reported by the ticker |
| `mark_price` | number | yes | Mark price |
| `index_price` | number | yes | Index price |
| `bid1_price` | number | yes | Best bid price |
| `bid1_size` | number | yes | Quantity available at the best bid |
| `ask1_price` | number | yes | Best ask price |
| `ask1_size` | number | yes | Quantity available at the best ask |
| `funding_rate` | number | yes | Funding rate as a fraction |
| `open_interest` | number | yes | Bybit open-interest size, both sides |
| `volume_24h` | number | yes | Rolling 24-hour traded quantity |
| `turnover_24h` | number | yes | Rolling 24-hour traded value |
| `created_at` | string (date-time) | yes | Database insertion time in UTC |

Example:

```json
{
  "id": 42,
  "timestamp": "2026-07-20T10:15:00.123456Z",
  "symbol": "BTCUSDT",
  "last_price": 118000.5,
  "mark_price": 118001.0,
  "index_price": 117999.0,
  "bid1_price": 118000.0,
  "bid1_size": 1.2,
  "ask1_price": 118000.5,
  "ask1_size": 0.8,
  "funding_rate": 0.0001,
  "open_interest": 12345.0,
  "volume_24h": 1200000000.0,
  "turnover_24h": 98000000000.0,
  "created_at": "2026-07-20T10:15:00.200000Z"
}
```

Treat `id` as opaque. It is useful only as a deterministic tie-breaker within
the current database; it is not a global identifier and may change after data
reimport, migration, or restore.

---

### `GET /ticker/{symbol}/latest`

Returns the newest row with a non-null `timestamp` for the exact symbol, ordered
by `timestamp DESC, id DESC`. Market fields, including `last_price`, can still
be `null`.

If no row with a usable timestamp exists:

```http
HTTP/1.1 404 Not Found
Content-Type: application/json
```

```json
{"detail":"Ticker data not found for symbol NOPEUSDT"}
```

---

### `GET /ticker/{symbol}/history`

Returns recent rows, newest first (`timestamp DESC, id DESC`).

| Query parameter | Type | Default | Rules |
|---|---|---:|---|
| `limit` | integer | `100` | `1…1000` |
| `from` | date-time | — | Optional inclusive lower bound |
| `to` | date-time | — | Optional inclusive upper bound |

Example:

```json
{
  "symbol": "BTCUSDT",
  "count": 1,
  "items": [
    {
      "id": 42,
      "timestamp": "2026-07-20T10:15:00.123456Z",
      "symbol": "BTCUSDT",
      "last_price": 118000.5,
      "mark_price": 118001.0,
      "index_price": 117999.0,
      "bid1_price": 118000.0,
      "bid1_size": 1.2,
      "ask1_price": 118000.5,
      "ask1_size": 0.8,
      "funding_rate": 0.0001,
      "open_interest": 12345.0,
      "volume_24h": 1200000000.0,
      "turnover_24h": 98000000000.0,
      "created_at": "2026-07-20T10:15:00.200000Z"
    }
  ]
}
```

`count` is the number of elements in this response's `items`, not the total
number of matching database rows.

- Unknown symbol: `200` with `count: 0` and `items: []`.
- `from > to`: `422`.
- This endpoint has no cursor or offset pagination. It returns at most the
  newest `limit` matching rows. Use `/ticker_data` for a complete range export.

---

### `GET /ticker_data` — paginated bulk export

| Query parameter | Type | Required | Rules |
|---|---|---:|---|
| `symbol` | string | yes | Exact value returned by `/symbols` |
| `start` | date-time | yes | Inclusive |
| `end` | date-time | yes | Inclusive; `start <= end` |
| `limit` | integer | no | Default `10000`; range `1…10000` |
| `cursor` | string | no | Opaque `next_cursor` from the preceding page |

Rows are returned in stable ascending order:
`timestamp ASC, id ASC`.

Each bulk row contains:

| Field | JSON type | Nullable |
|---|---|---:|
| `symbol` | string | yes |
| `timestamp` | string (date-time) | yes |
| `last_price` | number | yes |
| `mark_price` | number | yes |
| `bid1_price` | number | yes |
| `ask1_price` | number | yes |
| `funding_rate` | number | yes |

The schema is deliberately nullable so one legacy or damaged row does not make
an entire export fail. Under normal operation, returned rows have non-null
`symbol`, `timestamp`, and `last_price`; clients must still define their own
handling for unexpected null market values.

Example response for a page that has more rows:

```json
{
  "rows": [
    {
      "symbol": "BTCUSDT",
      "timestamp": "2026-07-20T10:00:00Z",
      "last_price": 118000.5,
      "mark_price": 118001.0,
      "bid1_price": 118000.0,
      "ask1_price": 118000.5,
      "funding_rate": 0.0001
    }
  ],
  "next_cursor": "<opaque-cursor>"
}
```

An empty or exhausted range returns:

```json
{"rows":[],"next_cursor":null}
```

#### Pagination protocol

1. Make the first request without `cursor`.
2. Process `rows` in the returned order.
3. If `next_cursor` is not `null`, repeat the request with exactly the same
   `symbol`, `start`, `end`, and `limit`, passing `cursor=next_cursor`.
4. Stop when `next_cursor` is `null`.
5. Treat the cursor as opaque. Store and replay it verbatim; never decode,
   modify, or construct one.
6. Pass the cursor through the HTTP library's query-parameter encoder.
7. Persist a complete page and its `next_cursor` atomically when possible. Do
   not advance the checkpoint after processing only part of a page.

A cursor rejected by the server normally returns:

```http
HTTP/1.1 422 Unprocessable Entity
```

```json
{"detail":"invalid cursor"}
```

A cursor is valid only for the active export walk and the original `symbol`,
`start`, `end`, and `limit`. Do not use it as a durable checkpoint for a later
export or replay it with different parameters.

Pagination is keyset-based but does not create a database snapshot. Rows added
or removed while an export is in progress can affect later pages. For a
repeatable historical export, choose an `end` time that is no longer being
written and avoid running retention cleanup during the walk.

Consequently, retrying the same cursor is safe with respect to server state but
is not guaranteed to return a byte-for-byte identical page while data is being
inserted or removed. A row inserted late behind the current cursor can be
missed. Clients should consume only fully decoded response bodies and make page
storage idempotent or transactional. If page storage and cursor advancement
cannot be atomic, processing has at-least-once delivery semantics and may need
client-specific duplicate handling.

---

### `GET /klines` — derived 1-minute OHLC

| Query parameter | Type | Required | Rules |
|---|---|---:|---|
| `symbol` | string | yes | Exact value returned by `/symbols` |
| `start` | date-time | yes | Inclusive |
| `end` | date-time | yes | Inclusive; `start <= end` |
| `interval` | string | no | Only `"1m"`; default `"1m"` |

Rows are ordered by `start_time ASC`.

```json
{
  "rows": [
    {
      "start_time": "2026-07-20T10:00:00Z",
      "open": 100.0,
      "high": 105.0,
      "low": 99.0,
      "close": 103.0
    }
  ]
}
```

All five fields in a returned kline row are non-null.

#### Semantics and limitations

- Candles are calculated from collected `last_price` ticker updates grouped
  into whole UTC minutes. They are not built from trades or Bybit's kline feed.
- Every returned `start_time` is the exact start of a whole UTC minute
  (`second=0`, no fractional seconds). Each minute appears at most once, so the
  documented ascending order is strict. Missing minutes remain valid gaps.
- OHLC values are a best-effort approximation and will not exactly match
  Bybit's official 1-minute trade candles.
- The collector stores change-gated ticker observations. A returned candle
  means at least one usable observation existed in that minute; it does not
  prove continuous market-data coverage for the whole minute.
- A minute `m` is emitted only when its entire `[m, m+1 minute)` window lies
  inside the requested `[start, end]` range. Partially covered edge minutes are
  omitted. Clients should use minute-aligned bounds.
- Minutes without a non-null `last_price` observation are omitted. The API does
  not create empty candles or forward-fill gaps.
- Unknown symbols and ranges with no complete usable minutes return
  `200 {"rows":[]}`.
- The maximum request window is currently `1440` minutes (24 hours). A larger
  window returns `422 {"detail":"range too large"}`. Split longer ranges into
  minute-aligned requests of at most 24 hours.

---

## Errors

| Status | Meaning | Client action |
|---:|---|---|
| `401` | Missing or invalid Bearer token | Do not retry unchanged credentials |
| `404` | No latest row, or unknown route | Treat as terminal for that request |
| `405` | HTTP method is not supported | Correct the client request |
| `422` | Missing/invalid query, bounds, limit, interval, range, or cursor | Correct the request; do not retry unchanged |
| `429` | Infrastructure rate limit, if configured | Honor `Retry-After`, then retry with backoff |
| `500–599` | Server or infrastructure failure | Retry with bounded exponential backoff and jitter |

Handled application errors use a string `detail`:

```json
{"detail":"start must be <= end"}
```

Framework-level validation errors use an array:

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["query", "symbol"],
      "msg": "Field required",
      "input": null
    }
  ]
}
```

Clients must support both forms. Responses generated by a reverse proxy or
other infrastructure are not guaranteed to have a JSON body, including `429`,
`502`, `503`, and `504`. Error handling must therefore check status and
`Content-Type` before attempting to parse JSON.

Authentication is evaluated independently of endpoint validation. A protected
malformed request without valid credentials can return `401` before any query
error is reported.

---

## Timeouts, retries, and concurrency

- Set explicit connection and response timeouts; do not rely on an HTTP
  library's unlimited default.
- Retry connection failures, `429`, and `5xx` using bounded exponential backoff
  with jitter.
- Do not automatically retry `401`, `404`, or `422` without changing the
  request.
- Reuse the same bulk cursor and all original query parameters when retrying a
  page.
- GET requests do not mutate server state, but live market responses can change
  between attempts.
- No application-level rate limit is currently advertised. Clients should still
  avoid unbounded parallel requests because bulk and kline queries read the
  shared market database.

Browser cross-origin access is not part of this contract. A browser client
requires an explicitly configured CORS policy and must not contain the shared
API key.

---

## Minimal Bash examples

Replace the example host with the value supplied by the API operator and enter
the supplied key at the prompt. Quoting is required; literal `<...>`
placeholders are not valid shell values.

```bash
BASE='https://market-api.example.com'
# Read without echoing the key or storing it in shell history.
IFS= read -rsp 'API key: ' KEY
printf '\n'

# Liveness is public; do not send the key.
curl --fail-with-body --silent --show-error \
  -H 'Accept: application/json' \
  "$BASE/health"

curl --fail-with-body --silent --show-error \
  -H 'Accept: application/json' \
  -H "Authorization: Bearer $KEY" \
  "$BASE/symbols"

curl --fail-with-body --silent --show-error \
  -H 'Accept: application/json' \
  -H "Authorization: Bearer $KEY" \
  "$BASE/ticker/BTCUSDT/latest"

curl --fail-with-body --silent --show-error --get \
  -H 'Accept: application/json' \
  -H "Authorization: Bearer $KEY" \
  --data-urlencode 'limit=100' \
  --data-urlencode 'from=2026-07-20T10:00:00Z' \
  --data-urlencode 'to=2026-07-20T11:00:00Z' \
  "$BASE/ticker/BTCUSDT/history"

# Bulk first page. Repeat with cursor=<next_cursor> until it is null.
curl --fail-with-body --silent --show-error --get \
  -H 'Accept: application/json' \
  -H "Authorization: Bearer $KEY" \
  --data-urlencode 'symbol=BTCUSDT' \
  --data-urlencode 'start=2026-07-20T10:00:00Z' \
  --data-urlencode 'end=2026-07-20T11:00:00Z' \
  --data-urlencode 'limit=10000' \
  "$BASE/ticker_data"

curl --fail-with-body --silent --show-error --get \
  -H 'Accept: application/json' \
  -H "Authorization: Bearer $KEY" \
  --data-urlencode 'symbol=BTCUSDT' \
  --data-urlencode 'start=2026-07-20T10:00:00Z' \
  --data-urlencode 'end=2026-07-20T11:00:00Z' \
  --data-urlencode 'interval=1m' \
  "$BASE/klines"
```
