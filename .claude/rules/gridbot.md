---
paths:
  - "apps/gridbot/**"
---

## gridbot — Live Trading Bot

**Path**: `apps/gridbot/`

### Architecture

- Single process, all accounts
- YAML config, hybrid event loop (async WebSocket + periodic polling)
- Data flow: `WebSocket → Orchestrator → StrategyRunner → GridEngine.on_event() → Intents → Executor → Bybit REST`
- Shadow mode: `shadow_mode=True` → intents logged, not executed; returns `shadow_{client_order_id}`

### Startup reconciliation is fail-closed (Feature 0086, issue #206)

`start()` retries a failed startup reconciliation in place (backoffs
`_STARTUP_RECONCILE_BACKOFFS = (2.0, 5.0, 10.0)`, 4 attempts total per
strategy). On exhaustion it emits a notifier alert
(`startup_reconcile_<strat_id>`) and raises `StartupReconciliationError` —
the process exits 1 with ZERO orders placed; any pre-existing exchange grid
is left untouched. Rationale: the bot must never trade on an unconfirmed
open-order book — a startup with empty local state places a duplicate grid
over live legacy orders. Same-price duplicates now self-heal within one
ticker (feature 0087, issue #220): `engine.py:_place_grid_orders` groups
limits into round-8 price buckets, keeps one survivor per price (preference:
grid-side match > fill history > first-in-list) and cancels the shadowed
extras with `CancelIntent(reason='duplicate')` — the proactive complement
to the reactive 0031/0046 post-fill layer. While the SAME ORDER soft-block is
latched (`runner._same_order_error`), only healing `CancelIntent(reason=
'duplicate')`s still execute via `_execute_generated_intents` (otherwise 0087
cleanup is a no-op during the latch); placements AND other-reason cancels
(rebuild/side_mismatch/outside_grid) stay suppressed — cancelling grid orders
without their paired placements would thin the grid. Mid-run order sync keeps
warn-and-retry semantics (stopping a bot managing a live grid is riskier);
sync failures now also alert (`order_sync_<strat_id>`, both `result.errors`
and exception paths). Repro/regression: `test_issue_206_startup_reconcile_race.py`,
`TestStartupReconcileRetry` in `test_orchestrator.py`.

### Health status file — check health without tailing logs (Feature 0082, issue #185)

The bot writes a machine-readable JSON snapshot to `status_file_path` (default
`/tmp/gridbot_status.json`, config key; `status_file_enabled=false` disables) every
~10s health sweep and once as `state="starting"` at the end of `start()`. Read it
with `jq . /tmp/gridbot_status.json` (or `jq .state ...`) for an instant read — no
log parsing. Example:

```json
{
  "state": "healthy",
  "generated_at": "2026-06-18T12:00:00+00:00",
  "strategies": [
    {"strat_id": "ltcusdt_test", "symbol": "LTCUSDT", "state": "healthy",
     "shadow": false, "net_position_size": 1.2, "preflight_skips": 0}
  ],
  "metrics": {"orders_placed": 42, "orders_placed_shadow": 0, "orders_rejected": {},
              "cancels": 7, "cancels_failed": 0, "rest_errors_by_code": {},
              "ws_reconnects": {}},
  "gauges": {"runners": 1, "auth_cooldown_active": 0, "loss_breaker_latched": 0,
             "preflight_skips": 0, "auth_cooldown_cycles": 0, "uptime_seconds": 3600.0}
}
```

- **`state`** — worst-wins overall: `circuit_open` (C3 loss breaker latched) >
  `auth_cooldown` > `degraded` (dirty-REST failures / C4 rate-limit) >
  `healthy`; `starting` pre-loop. Per-strat breakdown under `strategies[]` (with a
  `shadow` flag — live↔shadow snapshot shape is identical).
- **`metrics`** — process-lifetime monotonic counters (reset on restart): orders
  placed / placed_shadow / rejected-by-reason, `rest_errors_by_code`, cancels /
  cancels_failed, `ws_reconnects`. **`gauges`** — point-in-time: runners, auth-cooldown active/cycles,
  loss-breaker latched, preflight skips, uptime.
- Last-value-wins snapshot, NOT a time series — no history/rotation (non-goal: no
  Prometheus). Additive, no trading impact. **Complements** the `gridbot-health` skill
  (which owns the durable cross-restart `health_state.json` ledger); this file does
  not touch `health_state.json`.

### Key Patterns

- **Order tracking**: `TrackedOrder` dataclass, deterministic 16-char hex `client_order_id`
- **Position risk**: `StrategyRunner` owns linked `Position` pair; periodic check (63s default)
- **Event routing**: `_symbol_to_runners` (ticker), `_account_to_runners` (position/order/execution)
- **Reconciliation**: Startup (adopt existing orders) + reconnect (compare exchange vs in-memory) + periodic (61s, `order_sync_interval`)
- **Wallet caching**: `wallet_cache_interval` (300s default), reduces API calls ~79%
- **Position updates**: WebSocket-first, REST fallback (`_position_ws_data` cache)

### Exception Handling

Two-layer: Runner logs + re-raises → Orchestrator catches + sends Telegram alert via `Notifier`.

### Telegram Notifier

- Config: `notification.telegram.bot_token` + `chat_id` in YAML
- Throttle: 1 alert per error key per 60s
- Thread-safe (daemon thread), graceful degradation if not configured
- Dependency: `pytelegrambotapi>=4.24.0`

### Embedded EventSaver (`--save-events`)

- CLI flag `--save-events` or config `enable_event_saver: true` starts an embedded `EventSaver` alongside the trading bot
- EventSaver maintains its own WS connections (separate from orchestrator's) for raw data capture
- Startup order matters: Run records → EventSaver → gridbot WS connect (no capture gap)
- `_create_run_records()` creates User/BybitAccount/Strategy/Run rows with deterministic UUIDs via `uuid5(namespace, "type:name")`
- `_run_ids` dict is keyed by `strat_id` (not account name) — Run is strategy-scoped
- **Multi-strategy accounts**: `run_id` is set to `None` because `AccountContext` is account-scoped but `Run` is strategy-scoped. Executions/orders are captured but not persisted to DB. Positions/wallet/tickers still work. Fixing this requires per-symbol run_id mapping in EventSaver's normalizer pipeline.
- Accounts with zero strategies are skipped (empty `symbols=[]` means "no filter" in `PrivateCollector`, which would over-collect)
- Plan/review docs: `docs/features/0014_PLAN.md`, `docs/features/0014_REVIEW.md`
- **Debug walkthrough (architecture + breakpoint checklist)**: `docs/architecture/gridbot-save-events-debug.md`

### Key Pitfalls

- **`PositionState.margin` is a RATIO** (`positionValue / walletBalance`), NOT Bybit's `positionIM` dollar amount
- `PositionState.direction` is required
- Retry queue needs `_dispatch_intent()` closure to route Cancel vs Place correctly
- `asyncio.CancelledError` is `BaseException` — passes through `except Exception`
- Snapshot mutable dicts with `list(d.items())` before async iteration

### Reconciliation & order-adoption invariants (Phase E)

- **Inject is NOT durable adoption (bbu2-faithful)**: Injected orders live for exactly one ticker event. On the first `on_ticker` after startup, `GridEngine._place_grid_orders` (`packages/gridcore/src/gridcore/engine.py:319-325`) cancels any injected order whose price is not in the current `grid_price_set` (`'outside_grid'` reason), and `engine.py:305-312` cancels any at a grid price with the wrong side (`'side_mismatch'` reason). Over-limit cases (`engine.py:237-243`) trigger a full rebuild that cancels everything. Direct port of bbu2 `strat.py:154-160`, `:145-149`, `:103-104`. This means: (a) a "silent adoption of manual orders" security review is a false alarm — the bot does not keep manual orders around, it destroys them on the next tick; (b) the **real** operational concern is the opposite — the bot will **cancel** any limit order on the symbol that doesn't match the grid; (c) do NOT add a refuse-to-start check in `reconcile_startup` — it would re-break normal crash-restart (the bot's own prior orders look identical to manual ones) and was already removed in commit `138737a` for that reason.
- **(account, symbol) uniqueness is enforced unconditionally at config load**: Even though `orderLinkId` IS sent to Bybit (since feature 0029, with a `-{millis}` suffix added in HOTFIX 2026-05-08), it cannot disambiguate strategies at runtime. The deterministic prefix is a SHA of `(symbol, side, price, direction)`, so two strategies on the same `(account, symbol)` would compute the SAME prefix for the same logical order — the wire-form suffix only differs across re-placements, not across strategies. Two strategies on the same `(account, symbol)` pair would therefore cancel each other's orders every tick via the cancel-on-mismatch pass described above. `GridbotConfig.validate_no_shared_symbol` (`apps/gridbot/src/gridbot/config.py`) rejects any such configuration at load time with **no escape hatch** — there is no flag to disable it. bbu2 enforces the same invariant structurally: its `amounts[].strat` field is a scalar pointing at a single `pair_timeframes[]` entry, and each `pair_timeframe` has a single `symbol`, so the bad configuration is physically unrepresentable in bbu2's config schema. grid-bot's schema is more flexible (independent `accounts` and `strategies` lists, FK goes `strategy.account → account.name`), so the constraint must be reconstructed as a pydantic validator — but it is enforced just as strictly. If you need a second strategy on the same symbol, use a different account.
- **Operational consequence (manual orders get cancelled)**: Any limit order on the symbol that is not at a current grid price, or is at a grid price with the wrong side, will be cancelled by the engine on the next ticker event after it is seen (see the "Inject is NOT durable adoption" bullet above for the exact mechanism). This applies to manual orders placed via the Bybit UI while the bot is running, orders from other tools/scripts on the same account, and stale orders left over from a prior run with different grid parameters. **Manual orders and the grid cannot coexist on the same symbol** — the bot treats "not in my grid" as "cancel it." To manually intervene, stop the bot, make your changes, restart, and accept that anything not matching the grid on restart will be cancelled on the first tick.
- **Before first start**: Closing existing orders for the symbol before the first start is recommended for operator clarity (otherwise the bot will cancel them within ~1 second of startup, which is surprising but not incorrect). There is no config flag to disable either the cancel-on-mismatch behavior or the `(account, symbol)` uniqueness check — both are unconditional.

### orderLinkId wire format & matching

How the deterministic `client_order_id` survives Bybit's id-cache (HOTFIX 2026-05-08):

- **Why the suffix exists**: Bybit caches `orderLinkId` for ~1-2h post-cancel/fill. Our `PlaceLimitIntent.client_order_id` is a deterministic 16-char SHA256 hex digest of `(symbol, side, price, direction)`, so re-placing the same logical intent collides with the cached id and triggers ErrCode 110072 "OrderLinkedID is duplicate" in a tight loop. Live-verified: ~12k duplicate-rejected attempts / 0 successful orders across a 2h window before the fix.
- **Wire format**: `{16-hex prefix}-{int(datetime.now(UTC).timestamp() * 1000)}`. The prefix is guaranteed not to contain `-` (`hashlib.sha256().hexdigest()[:16]` returns only `0-9a-f`), so splitting at the first `-` always recovers the deterministic prefix.
- **Wire-vs-key invariant**: The full suffixed value goes on the wire and is persisted verbatim in `private_executions.order_link_id` and `orders.order_link_id` (forensics). Internal dict keys (`Runner._tracked_orders`, comparator join key, replay seed `client_id`) use only the deterministic prefix.
- **Retry idempotency invariant (feature 0032)**: The wire suffix is minted once per `PlaceLimitIntent` placement lifecycle in `StrategyRunner` and stored on `PlaceLimitIntent.order_link_id`; runner reattempts, retry-queue retries, and fresh engine re-emissions after a failed placement reuse that same wire id. Executor-side generation is only a fallback for direct callers that bypass runner assignment.
- **Reconcile-upgrade path**: If REST order sync later reports an open order whose normalized prefix matches a pending/failed tracked placement, `Runner.inject_open_orders` upgrades that tracked order to `placed`, patches the tracked intent with the exchange-reported wire `orderLinkId`, and cancels queued retries for that prefix. This closes the ambiguous-failure window where Bybit accepted the first request but the bot only observed a timeout/error. **Feature 0080 migration**: when the exchange wire prefix is pre-salt (`strat_id=None` hash) but the failed tracked placement used the salted `client_order_id`, upgrade by order identity `(price, qty, side, reduce_only)` and re-key to the exchange prefix — otherwise a queued retry can double-place after delayed reconcile (`runner._find_failed_tracked_by_order_identity`). `retry_dispatch_place` also re-checks `_is_good_to_place` and drops `duplicate_order_blocked` retries.
- **Helper**: `gridcore.intents.extract_client_order_prefix(order_link_id) -> Optional[str]` splits at the first `-` and returns the prefix. `None` or empty-string input → `None` (so callers using `prefix or fallback_id` cleanly fall back). No-hyphen input → unchanged (pre-hotfix backward compat).
- **Three call sites normalize on read**: (a) `gridbot.runner._find_tracked_order` and `inject_open_orders` — strip suffix before lookup/inject; (b) `comparator.loader.LiveTradeLoader.load` — strip before grouping live executions; (c) `replay.snapshot_loader.load_active_orders` — strip before seeding active orders for replay. Tests in `packages/gridcore/tests/test_intents.py`, `apps/gridbot/tests/test_runner.py`, `apps/comparator/tests/test_loader.py`, `apps/replay/tests/test_snapshot_loader.py`.
- **Files**: `packages/gridcore/src/gridcore/intents.py` (helper + `PlaceLimitIntent.order_link_id`), `apps/gridbot/src/gridbot/order_link_id.py`, `apps/gridbot/src/gridbot/executor.py` (wire-id fallback), `apps/gridbot/src/gridbot/runner.py` (wire-id assignment/reuse + read-side normalization), `apps/comparator/src/comparator/loader.py`, `apps/replay/src/replay/snapshot_loader.py`.

### Active WS reconnect with TCP-level probe (feature 0024)

bbu2 `_ensure_*_connection` pattern (2026-05-03):

- **Problem**: Wrapper's `is_connected()` is state-flag based — flips False only on explicit `disconnect()`. A dead TCP socket pybit hadn't noticed left it stuck True. Mainnet observed 6–15 min reconnect gaps.
- **Two health signals (both call `client.reset()`)**:
  - **Primary** (TCP-level, every 10s): `Orchestrator._ws_health_check_once()` calls new `client.is_socket_alive()` → pybit's `ws.sock.connected`. Mirrors bbu2 `ENSURE_SOCKET_INTERVAL = 10`.
  - **Secondary** (message-gap, on heartbeat fire): existing 30s gap detector → `on_disconnect` callback → `Orchestrator._on_ws_disconnect()` → `client.reset()`. Catches "socket alive but server silent" failure mode that TCP check misses.
- **`reset()`**: Stop heartbeat → `_disconnect_internal()` → `connect()` (re-subscribes all streams). Idempotent — back-to-back resets are a no-op + a single re-establishment.
- **Heartbeat thread sharp edge**: `on_disconnect` callback runs on the heartbeat thread. **Wrapper-level guard**: `_stop_heartbeat_watchdog` skips `Thread.join()` when `threading.current_thread() is self._heartbeat_thread`, so calling `reset()` inline from a callback is safe (no `RuntimeError`). The orchestrator still dispatches reset to a one-shot daemon worker (`WSReset-{account}-{kind}`) to avoid blocking the heartbeat thread on the full TCP teardown / handshake / subscription replay.
- **Zombie heartbeat protection**: `_start_heartbeat_watchdog` replaces `self._stop_heartbeat` with a fresh `threading.Event` each start; the old loop holds a reference to the old (still-set) event and exits cleanly. `_heartbeat_loop(stop_event)` takes the event as parameter.
- **`retries=0`**: Both `connect()` methods pass `retries=0` to `pybit.unified_trading.WebSocket(...)` → pybit's `infinitely_reconnect=True`. Removes the 10-attempt cliff at which pybit raises `WebSocketTimeoutException` and gives up.
- **Orchestrator wiring**: `_init_account` constructs WS clients with `on_disconnect=lambda ts, a=name: self._on_ws_disconnect(a, "public"|"private", ts)`. Periodic gate `_next_ws_health_check` in `_tick()` between `_next_health_check` and `_next_order_sync`.
- Files: `packages/bybit_adapter/src/bybit_adapter/ws_client.py`, `apps/gridbot/src/gridbot/orchestrator.py`

---

