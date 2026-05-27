"""Core data recorder orchestrator.

Coordinates WebSocket data collection and persistence for
standalone mainnet recording sessions.
"""

import asyncio
import logging
import signal
import threading
from concurrent.futures import Future
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from bybit_adapter.rest_client import BybitRestClient
from grid_db import (
    DatabaseFactory,
    User,
    BybitAccount,
    Strategy,
    Run,
    Order,
    OrderRepository,
    PositionSnapshot,
    PositionSnapshotRepository,
    WalletSnapshot,
    WalletSnapshotRepository,
)
from grid_db._decimal import WALLET_ACCOUNT_JSON_KEYS, decimal_or_zero
from grid_db.identity import account_id_for, strategy_id_for, user_id_for
from gridcore.events import PublicTradeEvent, ExecutionEvent, OrderUpdateEvent, TickerEvent

from event_saver.collectors import PublicCollector, PrivateCollector, AccountContext
from event_saver.writers import (
    TradeWriter,
    TickerWriter,
    ExecutionWriter,
    OrderWriter,
    PositionWriter,
    WalletWriter,
)
from event_saver.reconciler import GapReconciler

from recorder.config import RecorderConfig
from recorder.shared_db_parents import verify_shared_db_parents


logger = logging.getLogger(__name__)


class Recorder:
    """Standalone data recorder for Bybit mainnet capture.

    Simpler than EventSaver: no multi-tenant lifecycle, no run_id
    management. Single optional account, configured at startup.

    Example:
        config = load_config("recorder.yaml")
        db = DatabaseFactory(settings)
        recorder = Recorder(config=config, db=db)
        await recorder.start()
        await recorder.run_until_shutdown()
    """

    def __init__(self, config: RecorderConfig, db: DatabaseFactory):
        self._config = config
        self._db = db

        # Collectors
        self._public_collector: Optional[PublicCollector] = None
        self._private_collector: Optional[PrivateCollector] = None

        # Writers
        self._trade_writer: Optional[TradeWriter] = None
        self._ticker_writer: Optional[TickerWriter] = None
        self._execution_writer: Optional[ExecutionWriter] = None
        self._order_writer: Optional[OrderWriter] = None
        self._position_writer: Optional[PositionWriter] = None
        self._wallet_writer: Optional[WalletWriter] = None

        # Infrastructure
        self._reconciler: Optional[GapReconciler] = None
        self._health_task: Optional[asyncio.Task] = None

        # State
        self._running = False
        self._start_time: Optional[datetime] = None
        self._shutdown_event = asyncio.Event()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._gap_count = 0
        self._gap_lock = threading.Lock()
        self._run_id: Optional[UUID] = None
        self._health_check_complete = asyncio.Event()

        # Identity attrs — sentinels overwritten by _seed_db_records when
        # config.account is set (shared-DB / Phase 4 mode). Fallback mode
        # (no account: block) keeps these legacy placeholder UUIDs so the
        # standalone recorder path is unchanged. Declared here so cleanup /
        # except paths never hit AttributeError on partial init.
        self._account_id: UUID = UUID("00000000-0000-0000-0000-000000000002")
        self._user_id: UUID = UUID("00000000-0000-0000-0000-000000000001")
        self._strategy_id: UUID = UUID("00000000-0000-0000-0000-000000000003")

    async def start(self) -> None:
        """Start all recording components."""
        if self._running:
            logger.warning("Recorder already running")
            return

        # Set _running early so stop() can clean up if start() raises
        # partway through (e.g. after writers are started but before
        # collectors connect).  Without this, stop() would no-op and
        # leave orphaned writer flush-loop tasks.
        self._running = True

        try:
            self._shutdown_event.clear()

            if not self._config.symbols:
                raise ValueError("symbols must not be empty")

            logger.info("Starting Recorder...")
            self._start_time = datetime.now(UTC)
            self._event_loop = asyncio.get_running_loop()

            # Initialize REST client for reconciliation (public endpoints only;
            # empty credentials are intentional — no auth needed).
            rest_client = BybitRestClient(
                api_key="",
                api_secret="",
                testnet=self._config.testnet,
            )

            # Initialize reconciler
            self._reconciler = GapReconciler(
                db=self._db,
                rest_client=rest_client,
                gap_threshold_seconds=self._config.gap_threshold_seconds,
            )

            await self._init_writers()

            # 0029 Cross-cutting #4: write the t=0 row of the recording session
            # via REST BEFORE the private collector subscribes. Bybit's private
            # streams are event-driven, so a quiet account would otherwise leave
            # the seed-aware replay loader returning NULL for wallet/positions/
            # orders even though state existed live. Failures here are logged
            # but DO NOT abort recorder start (the WS stream still gets captured;
            # Phase 4's pre-check will refuse to seed from a run missing the
            # initial snapshot).
            if self._config.account:
                await self._write_initial_rest_snapshot()

            await self._init_collectors()

            # Start health logging
            self._health_task = asyncio.create_task(self._health_log_loop())

        except Exception:
            # _running stays True so stop(error=True) in main.py can
            # clean up any partially-initialized resources.
            raise

        logger.info(
            "Recorder started. "
            f"Symbols: {self._config.symbols}, "
            f"Testnet: {self._config.testnet}, "
            f"Private: {self._config.account is not None}"
        )

    async def _init_writers(self) -> None:
        """Create writers and start their background flush loops."""
        writer_kwargs = {
            "db": self._db,
            "batch_size": self._config.batch_size,
            "flush_interval": self._config.flush_interval,
        }

        # Always seed DB records and create a Run for this session.
        # Replay engine needs a Run row to discover the recording time range.
        self._run_id = await asyncio.to_thread(self._seed_db_records)

        self._ticker_writer = TickerWriter(**writer_kwargs)
        await self._ticker_writer.start_auto_flush()

        if self._config.capture_public_trades:
            self._trade_writer = TradeWriter(**writer_kwargs)
            await self._trade_writer.start_auto_flush()

        if self._config.account:
            # 0029: stamp run_id on every wallet/position row so seed-aware
            # replay can scope its lookups to one recorder run. Order rows
            # already carry run_id via OrderUpdateEvent.run_id; passing the
            # kwarg is a no-op for Order/Execution/Trade/Ticker writers and
            # is only consumed by Wallet/Position writers.
            run_id_str = str(self._run_id) if self._run_id else None
            self._execution_writer = ExecutionWriter(**writer_kwargs)
            self._order_writer = OrderWriter(**writer_kwargs)
            self._position_writer = PositionWriter(**writer_kwargs, run_id=run_id_str)
            self._wallet_writer = WalletWriter(**writer_kwargs, run_id=run_id_str)
            await self._execution_writer.start_auto_flush()
            await self._order_writer.start_auto_flush()
            await self._position_writer.start_auto_flush()
            await self._wallet_writer.start_auto_flush()

    async def _init_collectors(self) -> None:
        """Create and start public/private WebSocket collectors."""
        self._public_collector = PublicCollector(
            symbols=self._config.symbols,
            on_ticker=self._handle_ticker,
            on_trades=self._handle_trades if self._config.capture_public_trades else None,
            on_gap_detected=self._handle_public_gap,
            testnet=self._config.testnet,
        )
        await self._public_collector.start()

        if self._config.account:
            environment = "testnet" if self._config.testnet else "mainnet"
            context = AccountContext(
                account_id=self._account_id,
                user_id=self._user_id,
                run_id=self._run_id,
                api_key=self._config.account.api_key.get_secret_value(),
                api_secret=self._config.account.api_secret.get_secret_value(),
                environment=environment,
                symbols=self._config.symbols,
            )
            self._private_collector = PrivateCollector(
                context=context,
                on_execution=self._handle_execution,
                on_order=lambda event: self._handle_order(
                    self._account_id, event
                ),
                on_position=lambda msg: self._handle_position(
                    self._account_id, msg
                ),
                on_wallet=lambda msg: self._handle_wallet(
                    self._account_id, msg
                ),
                on_gap_detected=self._handle_private_gap,
            )
            await self._private_collector.start()

    async def _write_initial_rest_snapshot(self) -> None:
        """Write a one-shot REST snapshot as the t=0 row of the recording.

        0029 Cross-cutting #4. Bybit private streams are event-driven; a quiet
        account between recorder start and the first wallet/position/order
        change leaves the seed-aware replay loader returning NULL. This method
        fetches wallet, positions, and open orders via REST and persists them
        directly through the snapshot repositories so the loader's
        ``latest <= at_ts`` query always finds at least one row per dimension.

        Contract (per docs/features/0029_PLAN.md "Initial-snapshot row contract"):
        - WalletSnapshot: one row per coin returned by ``get_wallet_balance``
          (downstream filters to USDT; writing all coins is fine).
        - PositionSnapshot: ALWAYS two rows per configured symbol — one
          ``side='Buy'`` and one ``side='Sell'`` — even when the corresponding
          side is absent in the REST response (size=0, entry_price=0,
          liq_price=NULL). The "always two rows" invariant lets the loader
          treat exactly one side missing as ``SeedDataQualityError`` rather
          than a benign "no activity" case.
        - Order: one row per open order with ``status``, ``leaves_qty``,
          ``reduce_only``, ``order_link_id`` from the response. ``exchange_ts``
          and ``local_ts`` are the REST-call wall-clock (NOT the order's
          original ``createdTime``) so this snapshot row sorts BEFORE any
          subsequent WS-stream rows for the same ``order_id`` in this run.

        All rows are stamped with ``self._run_id`` so they share scope with
        subsequent stream rows.

        Errors per call are logged and swallowed: the recorder must still
        capture the WS stream even when REST is degraded.
        """
        if not self._config.account or not self._run_id:
            return

        # Authenticated REST client for private endpoints. The recorder's
        # existing self._reconciler client uses empty credentials (public
        # endpoints only), so a fresh client is required here. It is
        # one-shot — no need to retain.
        try:
            auth_client = BybitRestClient(
                api_key=self._config.account.api_key.get_secret_value(),
                api_secret=self._config.account.api_secret.get_secret_value(),
                testnet=self._config.testnet,
            )
        except Exception as e:
            logger.error(f"Failed to construct authenticated REST client for initial snapshot: {e}")
            return

        run_id_str = str(self._run_id)
        account_id_str = str(self._account_id)
        snapshot_ts = datetime.now(UTC)

        wallet_count = await self._snapshot_wallet(
            auth_client, run_id_str, account_id_str, snapshot_ts
        )
        position_count = await self._snapshot_positions(
            auth_client, run_id_str, account_id_str, snapshot_ts
        )
        order_count = await self._snapshot_open_orders(
            auth_client, run_id_str, account_id_str, snapshot_ts
        )

        logger.info(
            f"Initial REST snapshot: wallet={wallet_count} coins, "
            f"positions={position_count} rows, open_orders={order_count}"
        )

        # 0029 cross-cutting #4: empty wallet OR position dimension means
        # the seed loader will not find a t=0 row and Phase 4's pre-check
        # will refuse to seed from this run. Surface this loudly at recorder
        # start so an operator catches credential/permissions problems early
        # instead of finding out hours later when replay refuses. open_orders
        # legitimately can be zero (clean account) — not warned on.
        if wallet_count == 0 or position_count == 0:
            logger.warning(
                "Initial REST snapshot incomplete: "
                f"wallet_rows={wallet_count}, position_rows={position_count} "
                "(zero on either dimension means seed-aware replay from this "
                "run_id will fail Phase 4 pre-check; check API credentials / "
                "permissions / category=linear settleCoin=USDT scope)"
            )

    async def _snapshot_wallet(
        self,
        client: BybitRestClient,
        run_id: str,
        account_id: str,
        snapshot_ts: datetime,
    ) -> int:
        """REST-fetch wallet balance and write one row per coin. Returns row count."""
        try:
            result = await asyncio.to_thread(client.get_wallet_balance, "UNIFIED")
        except Exception as e:
            logger.error(f"Initial snapshot: get_wallet_balance failed: {e}")
            return 0

        # Bybit V5 shape: result["list"][0]["coin"] = list of per-coin dicts.
        accounts = result.get("list") or []
        snapshots: list[WalletSnapshot] = []
        for acct in accounts:
            try:
                account_raw = {
                    key: acct.get(key)
                    for key in WALLET_ACCOUNT_JSON_KEYS
                    if key in acct
                }
                total_equity = decimal_or_zero(acct.get("totalEquity"))
                total_available_balance = decimal_or_zero(
                    acct.get("totalAvailableBalance")
                )
                total_margin_balance = decimal_or_zero(acct.get("totalMarginBalance"))
                account_im_rate = decimal_or_zero(acct.get("accountIMRate"))
                account_mm_rate = decimal_or_zero(acct.get("accountMMRate"))
            except Exception as e:
                logger.warning(
                    f"Initial snapshot: skipped malformed wallet account row: {e}"
                )
                continue

            for coin_data in acct.get("coin") or []:
                try:
                    # UTA v5 returns `availableToWithdraw`; legacy UTA 1.0 and
                    # some non-USDT coins on cross-margin still surface only
                    # `availableBalance`. Prefer the v5 field, fall back to
                    # the legacy field when v5 is absent or empty.
                    coin_available = coin_data.get("availableToWithdraw")
                    if coin_available in (None, "") and "availableBalance" in coin_data:
                        coin_available = coin_data.get("availableBalance")
                    snapshots.append(
                        WalletSnapshot(
                            run_id=run_id,
                            account_id=account_id,
                            exchange_ts=snapshot_ts,
                            local_ts=snapshot_ts,
                            coin=coin_data.get("coin", ""),
                            wallet_balance=decimal_or_zero(
                                coin_data.get("walletBalance")
                            ),
                            available_balance=decimal_or_zero(coin_available),
                            total_equity=total_equity,
                            total_available_balance=total_available_balance,
                            total_margin_balance=total_margin_balance,
                            account_im_rate=account_im_rate,
                            account_mm_rate=account_mm_rate,
                            raw_json={**coin_data, "_account": account_raw},
                        )
                    )
                except Exception as e:
                    logger.warning(
                        f"Initial snapshot: skipped malformed wallet coin row: {e}"
                    )
                    continue

        if not snapshots:
            return 0

        try:
            await asyncio.to_thread(self._bulk_insert_wallet_snapshots, snapshots)
        except Exception as e:
            logger.error(f"Initial snapshot: wallet bulk_insert failed: {e}")
            return 0
        return len(snapshots)

    def _bulk_insert_wallet_snapshots(self, snapshots: list[WalletSnapshot]) -> None:
        with self._db.get_session() as session:
            WalletSnapshotRepository(session).bulk_insert(snapshots)

    async def _snapshot_positions(
        self,
        client: BybitRestClient,
        run_id: str,
        account_id: str,
        snapshot_ts: datetime,
    ) -> int:
        """REST-fetch positions and write BOTH sides per configured symbol.

        Contract: ALWAYS exactly two rows per symbol (Buy + Sell). When the
        REST response omits a side, write a zero-size row for it.
        """
        snapshots: list[PositionSnapshot] = []
        for symbol in self._config.symbols:
            try:
                positions = await asyncio.to_thread(client.get_positions, symbol)
            except Exception as e:
                logger.error(
                    f"Initial snapshot: get_positions({symbol}) failed: {e}"
                )
                # Still write zero-rows for both sides so the loader's
                # "exactly one side missing" check doesn't fire on a
                # transient REST failure.
                positions = []

            # Index by side for O(1) lookup.
            by_side: dict[str, dict] = {}
            for pos in positions:
                if pos.get("symbol") != symbol:
                    continue
                side = pos.get("side")
                if side in ("Buy", "Sell"):
                    by_side[side] = pos

            for side in ("Buy", "Sell"):
                pos = by_side.get(side)
                if pos is not None:
                    try:
                        snapshots.append(
                            PositionSnapshot(
                                run_id=run_id,
                                account_id=account_id,
                                symbol=symbol,
                                exchange_ts=snapshot_ts,
                                local_ts=snapshot_ts,
                                side=side,
                                size=Decimal(str(pos.get("size") or "0")),
                                entry_price=Decimal(
                                    str(pos.get("entryPrice") or pos.get("avgPrice") or "0")
                                ),
                                liq_price=(
                                    Decimal(str(pos.get("liqPrice")))
                                    if pos.get("liqPrice") not in (None, "", "0")
                                    else None
                                ),
                                unrealised_pnl=(
                                    Decimal(str(pos.get("unrealisedPnl")))
                                    if pos.get("unrealisedPnl") not in (None, "")
                                    else None
                                ),
                                # 0034: position telemetry parity columns.
                                # mark_price guard matches the WS path
                                # (position_writer.py): only None/"" → NULL.
                                # A genuine 0 mark is rare but valid and
                                # must be preserved for parity recomputation.
                                source="live",
                                mark_price=(
                                    Decimal(str(pos.get("markPrice")))
                                    if pos.get("markPrice") not in (None, "")
                                    else None
                                ),
                                position_im=(
                                    Decimal(str(pos.get("positionIM")))
                                    if pos.get("positionIM") not in (None, "")
                                    else None
                                ),
                                position_mm=(
                                    Decimal(str(pos.get("positionMM")))
                                    if pos.get("positionMM") not in (None, "")
                                    else None
                                ),
                                cum_realised_pnl=(
                                    Decimal(str(pos.get("cumRealisedPnl")))
                                    if pos.get("cumRealisedPnl") not in (None, "")
                                    else None
                                ),
                                raw_json=pos,
                            )
                        )
                        continue
                    except Exception as e:
                        logger.warning(
                            f"Initial snapshot: malformed position row "
                            f"({symbol} {side}); writing zero-row: {e}"
                        )
                # Absent (or malformed): write the contract zero-row.
                snapshots.append(
                    PositionSnapshot(
                        run_id=run_id,
                        account_id=account_id,
                        symbol=symbol,
                        exchange_ts=snapshot_ts,
                        local_ts=snapshot_ts,
                        side=side,
                        size=Decimal("0"),
                        entry_price=Decimal("0"),
                        liq_price=None,
                        unrealised_pnl=None,
                        # 0034: zero-row stays NULL for telemetry; source=live.
                        source="live",
                        mark_price=None,
                        position_im=None,
                        position_mm=None,
                        cum_realised_pnl=None,
                        raw_json=None,
                    )
                )

        if not snapshots:
            return 0

        try:
            await asyncio.to_thread(self._bulk_insert_position_snapshots, snapshots)
        except Exception as e:
            logger.error(f"Initial snapshot: position bulk_insert failed: {e}")
            return 0
        return len(snapshots)

    def _bulk_insert_position_snapshots(self, snapshots: list[PositionSnapshot]) -> None:
        with self._db.get_session() as session:
            PositionSnapshotRepository(session).bulk_insert(snapshots)

    async def _snapshot_open_orders(
        self,
        client: BybitRestClient,
        run_id: str,
        account_id: str,
        snapshot_ts: datetime,
    ) -> int:
        """REST-fetch open orders for configured symbols and write one row each.

        ``exchange_ts``/``local_ts`` are the snapshot timestamp (REST-call
        wall-clock), NOT the order's ``createdTime``. This guarantees the
        snapshot row sorts BEFORE any subsequent WS-stream rows for the same
        ``order_id`` in this run, so the loader's MAX(exchange_ts) GROUP BY
        order_id picks up a later WS state when one exists.
        """
        models: list[Order] = []
        for symbol in self._config.symbols:
            try:
                orders = await asyncio.to_thread(
                    client.get_open_orders, symbol, "Limit"
                )
            except Exception as e:
                logger.error(
                    f"Initial snapshot: get_open_orders({symbol}) failed: {e}"
                )
                continue

            for order in orders:
                try:
                    qty = Decimal(str(order.get("qty") or "0"))
                    cum_exec_qty = Decimal(str(order.get("cumExecQty") or "0"))
                    leaves_from_resp = order.get("leavesQty")
                    leaves_qty = (
                        Decimal(str(leaves_from_resp))
                        if leaves_from_resp not in (None, "")
                        else qty - cum_exec_qty
                    )
                    status = "PartiallyFilled" if cum_exec_qty > 0 else "New"
                    # If the response carries an explicit orderStatus, prefer it.
                    if order.get("orderStatus"):
                        status = order["orderStatus"]

                    order_link_id = order.get("orderLinkId") or None
                    reduce_only_raw = order.get("reduceOnly")
                    reduce_only = (
                        bool(reduce_only_raw)
                        if reduce_only_raw is not None
                        else False
                    )

                    models.append(
                        Order(
                            run_id=run_id,
                            account_id=account_id,
                            order_id=order.get("orderId", ""),
                            order_link_id=order_link_id,
                            symbol=order.get("symbol", symbol),
                            exchange_ts=snapshot_ts,
                            local_ts=snapshot_ts,
                            status=status,
                            side=order.get("side", ""),
                            price=Decimal(str(order.get("price") or "0")),
                            qty=qty,
                            leaves_qty=leaves_qty,
                            reduce_only=reduce_only,
                            raw_json=order,
                        )
                    )
                except Exception as e:
                    logger.warning(
                        f"Initial snapshot: skipped malformed open order: {e}"
                    )
                    continue

        if not models:
            return 0

        try:
            await asyncio.to_thread(self._bulk_insert_orders, models)
        except Exception as e:
            logger.error(f"Initial snapshot: order bulk_insert failed: {e}")
            return 0
        return len(models)

    def _bulk_insert_orders(self, models: list[Order]) -> None:
        with self._db.get_session() as session:
            OrderRepository(session).bulk_insert(models)

    async def stop(self, *, error: bool = False) -> None:
        """Stop all components gracefully.

        Args:
            error: If True, mark the DB run as 'error' instead of 'completed'.
        """
        if not self._running:
            return

        logger.info("Stopping Recorder...")
        self._running = False

        # Stop health logging
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

        # Stop collectors
        if self._public_collector:
            await self._public_collector.stop()

        if self._private_collector:
            await self._private_collector.stop()

        # GapReconciler is stateless (no background tasks, no held connections).
        # Drop the reference so no new reconciliation futures are scheduled
        # after stop; any in-flight REST futures will complete on their own.
        self._reconciler = None

        # Stop writers (flushes remaining buffers)
        for writer in [
            self._trade_writer,
            self._ticker_writer,
            self._execution_writer,
            self._order_writer,
            self._position_writer,
            self._wallet_writer,
        ]:
            if writer:
                await writer.stop()

        # Mark run status in DB
        status = "error" if error else "completed"
        await asyncio.to_thread(self._mark_run_status, status)

        # Log final stats
        stats = self.get_stats()
        logger.info(f"Recorder stopped. Final stats: {stats}")

    async def run_until_shutdown(self) -> None:
        """Run until SIGINT/SIGTERM received."""
        loop = asyncio.get_running_loop()

        def shutdown_handler():
            logger.info("Shutdown signal received")
            self._shutdown_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_handler)

        try:
            await self._shutdown_event.wait()
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
        await self.stop()

    def _seed_db_records(self) -> UUID:
        """Create / verify parent DB records and a Run for this session.

        Two modes:

        - **Shared-DB (Phase 4)**: `config.account` is set with `name` /
          `strat_id`. Derive uuid5 IDs matching gridbot's
          `_create_run_records`; verify gridbot's User / BybitAccount /
          Strategy rows exist with compatible metadata
          (`verify_shared_db_parents`); insert only the Run row. The
          recorder is a *consumer* of gridbot's parent rows — never a
          co-writer (PK-driven `session.merge` would silently mutate
          gridbot metadata).
        - **Fallback (standalone / ticker-only)**: keep legacy placeholder
          UUIDs and upsert recorder-owned User / BybitAccount / Strategy
          rows. No co-located gridbot expected.

        Returns:
            The run_id UUID for this recording session.
        """
        run_id = uuid4()
        environment = "testnet" if self._config.testnet else "mainnet"
        # Store first symbol in Strategy.symbol (VARCHAR(20) limit),
        # full list goes in config_json for reference.
        primary_symbol = self._config.symbols[0]

        if self._config.account is not None:
            account_name = self._config.account.name
            strat_id = self._config.account.strat_id
            self._account_id = UUID(account_id_for(account_name))
            self._user_id = UUID(user_id_for(account_name))
            self._strategy_id = UUID(strategy_id_for(strat_id))

        try:
            with self._db.get_session() as session:
                if self._config.account is not None:
                    # Shared-DB: verify gridbot parents exist (no upsert).
                    verify_shared_db_parents(
                        session,
                        user_id=str(self._user_id),
                        account_id=str(self._account_id),
                        strategy_id=str(self._strategy_id),
                        account_name=self._config.account.name,
                        strat_id=self._config.account.strat_id,
                        primary_symbol=primary_symbol,
                        recorder_testnet=self._config.testnet,
                    )
                else:
                    # Fallback: standalone recorder; upsert recorder-owned
                    # parent rows under the legacy placeholder UUIDs.
                    session.merge(User(
                        user_id=str(self._user_id),
                        username="recorder",
                    ))
                    session.merge(BybitAccount(
                        account_id=str(self._account_id),
                        user_id=str(self._user_id),
                        account_name="recorder",
                        environment=environment,
                    ))
                    session.merge(Strategy(
                        strategy_id=str(self._strategy_id),
                        account_id=str(self._account_id),
                        strategy_type="recorder",
                        symbol=primary_symbol,
                        config_json={
                            "mode": "recorder",
                            "symbols": self._config.symbols,
                        },
                    ))
                # Create new Run for this session — FK resolves to the
                # bootstrapped (shared-DB) or recorder-owned (fallback) parents.
                session.add(Run(
                    run_id=str(run_id),
                    user_id=str(self._user_id),
                    account_id=str(self._account_id),
                    strategy_id=str(self._strategy_id),
                    run_type="recording",
                    status="running",
                ))
        except Exception as e:
            raise RuntimeError(f"Failed to initialize recording session: {e}") from e

        logger.info(f"Created recording run: {run_id}")
        return run_id

    def _mark_run_status(self, status: str) -> None:
        """Mark the current Run's status in the database.

        Args:
            status: Run status to set (e.g. 'completed', 'error').
        """
        if not self._run_id:
            return
        try:
            with self._db.get_session() as session:
                run = session.get(Run, str(self._run_id))
                if run:
                    run.status = status
                    run.end_ts = datetime.now(UTC)
                    logger.info(f"Marked run {self._run_id} as {status}")
                else:
                    logger.warning(f"Run {self._run_id} not found in database")
        except Exception as e:
            logger.error(f"Failed to mark run {self._run_id} as {status}: {e}")

    @staticmethod
    def _log_future_error(label: str):
        """Return a done-callback that logs exceptions from fire-and-forget futures."""
        def _cb(future: Future) -> None:
            if (exc := future.exception()) is not None:
                logger.error("%s failed: %s", label, exc)
        return _cb

    def _handle_ticker(self, event: TickerEvent) -> Optional[Future]:
        """Route ticker event to writer."""
        if self._ticker_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._ticker_writer.write([event]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("ticker write"))
            return fut
        return None

    def _handle_trades(self, events: list[PublicTradeEvent]) -> Optional[Future]:
        """Route trade events to writer."""
        if self._trade_writer and events and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._trade_writer.write(events),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("trade write"))
            return fut
        return None

    def _handle_execution(self, event: ExecutionEvent) -> Optional[Future]:
        """Route execution event to writer."""
        if self._execution_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._execution_writer.write([event]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("execution write"))
            return fut
        return None

    def _handle_order(self, account_id: UUID, event: OrderUpdateEvent) -> Optional[Future]:
        """Route order event to writer."""
        if self._order_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._order_writer.write(account_id, [event]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("order write"))
            return fut
        return None

    def _handle_position(self, account_id: UUID, message: dict) -> Optional[Future]:
        """Route position snapshot to writer."""
        if self._position_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._position_writer.write(account_id, [message]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("position write"))
            return fut
        return None

    def _handle_wallet(self, account_id: UUID, message: dict) -> Optional[Future]:
        """Route wallet snapshot to writer."""
        if self._wallet_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._wallet_writer.write(account_id, [message]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("wallet write"))
            return fut
        return None

    def _handle_public_gap(
        self, symbol: str, gap_start: datetime, gap_end: datetime
    ) -> Optional[Future]:
        """Trigger REST reconciliation for public data gap."""
        # Count unconditionally; if reconciler is unavailable (e.g. after stop),
        # the gap is still tracked in stats but no REST backfill is triggered.
        with self._gap_lock:
            self._gap_count += 1
        if self._reconciler and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._reconciler.reconcile_public_trades(
                    symbol=symbol,
                    gap_start=gap_start,
                    gap_end=gap_end,
                ),
                self._event_loop,
            )
            fut.add_done_callback(
                self._log_future_error(f"public reconciliation ({symbol})")
            )
            return fut
        return None

    def _handle_private_gap(
        self, gap_start: datetime, gap_end: datetime
    ) -> list[Future]:
        """Reconcile private stream gap via REST API."""
        # Count unconditionally (see _handle_public_gap comment).
        with self._gap_lock:
            self._gap_count += 1
        gap_seconds = (gap_end - gap_start).total_seconds()
        logger.warning(
            f"Private stream gap detected: {gap_seconds:.1f}s "
            f"({gap_start} to {gap_end})"
        )

        futures: list[Future] = []
        if (
            self._reconciler
            and self._event_loop
            and self._config.account
            and self._run_id
        ):
            for symbol in self._config.symbols:
                fut = asyncio.run_coroutine_threadsafe(
                    self._reconciler.reconcile_executions(
                        user_id=self._user_id,
                        account_id=self._account_id,
                        run_id=self._run_id,
                        symbol=symbol,
                        gap_start=gap_start,
                        gap_end=gap_end,
                        api_key=self._config.account.api_key.get_secret_value(),
                        api_secret=self._config.account.api_secret.get_secret_value(),
                        testnet=self._config.testnet,
                    ),
                    self._event_loop,
                )
                fut.add_done_callback(
                    self._log_future_error(f"private reconciliation ({symbol})")
                )
                futures.append(fut)
        return futures

    async def _health_log_loop(self) -> None:
        """Periodically log health stats."""
        while self._running:
            try:
                self._health_check_complete.clear()
                await asyncio.sleep(self._config.health_log_interval)
                stats = self.get_stats()
                logger.info(f"Health: {stats}")
                self._health_check_complete.set()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health log loop: {e}")

    def get_stats(self) -> dict:
        """Get recorder statistics."""
        uptime = 0.0
        if self._start_time:
            uptime = (datetime.now(UTC) - self._start_time).total_seconds()

        with self._gap_lock:
            gap_count = self._gap_count

        stats = {
            "uptime_seconds": round(uptime, 1),
            "gaps_detected": gap_count,
        }

        # Public WS connection state
        if self._public_collector:
            conn_state = self._public_collector.get_connection_state()
            if conn_state:
                stats["public_ws"] = {
                    "connected": conn_state.is_connected,
                    "reconnect_count": conn_state.reconnect_count,
                }

        # Writer stats with message rates
        for name, writer in [
            ("trades", self._trade_writer),
            ("tickers", self._ticker_writer),
            ("executions", self._execution_writer),
            ("orders", self._order_writer),
            ("positions", self._position_writer),
            ("wallets", self._wallet_writer),
        ]:
            if writer:
                writer_stats = writer.get_stats()
                if uptime > 0:
                    writer_stats["msgs_per_sec"] = round(
                        writer_stats["total_written"] / uptime, 2
                    )
                stats[name] = writer_stats

        # Reconciler stats
        if self._reconciler:
            stats["reconciler"] = self._reconciler.get_stats()

        return stats
