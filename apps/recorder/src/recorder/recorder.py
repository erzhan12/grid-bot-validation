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
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional
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
    PrivateStreamGapRepository,
    PrivateStreamSessionRepository,
    RowNotFoundError,
)
from grid_db._decimal import WALLET_ACCOUNT_JSON_KEYS, decimal_or_zero
from grid_db.identity import account_id_for, strategy_id_for, user_id_for
from gridcore.events import PublicTradeEvent, ExecutionEvent, OrderUpdateEvent, TickerEvent

from event_saver.collectors import (
    LIVENESS_MARGIN,
    AccountContext,
    PrivateCollector,
    PublicCollector,
)
from event_saver.writers import (
    TradeWriter,
    TickerWriter,
    ExecutionWriter,
    OrderWriter,
    PositionWriter,
    WalletWriter,
    leg_side,
)
from event_saver.reconciler import GapReconciler, recovery_result_from_future

from recorder.config import RecorderConfig
from recorder.shared_db_parents import verify_shared_db_parents


logger = logging.getLogger(__name__)

# Consecutive open-gap write failures at which the socket reset goes ahead
# anyway (so the reset is skipped on at most this many minus one probes):
# orders, positions and wallet have no REST backfill, so a dead socket must
# not stay un-reset.
_MAX_OPEN_GAP_FAILURES = 3

_INITIAL_SNAPSHOT = "Initial snapshot"
_POST_GAP_SNAPSHOT = "Post-gap snapshot"
# The coin replay seeds its wallet from (replay SeedConfig.wallet_coin default).
_WALLET_SEED_COIN = "USDT"
# Placeholders that do not prove a leg's state (0110 B2a markers).
_UNPROVEN_SYNTHETIC = frozenset({"rest_failure", "malformed", "empty_response"})


def _is_flat_position_row(pos: dict) -> bool:
    """True when a REST position row's size is zero (``""`` counts as 0)."""
    try:
        return Decimal(str(pos.get("size") or "0")) == 0
    except (InvalidOperation, ValueError, TypeError):
        return False


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
        # Feature 0110 B1c-1 private-stream coverage. Private WS writes are
        # registered under _pending_lock (pybit thread) so a checkpoint
        # barrier sees every write submitted before it. The rest is touched
        # on the event loop only.
        self._pending_lock = threading.Lock()
        self._pending_futures: set[Future] = set()
        self._private_write_lost = False
        self._private_write_lost_at: Optional[datetime] = None
        self._private_session_id: Optional[int] = None
        self._open_gap_ids: dict[str, int] = {}
        self._open_gap_start: Optional[datetime] = None
        self._open_gap_failures = 0
        self._pending_gap_writes: list[tuple[Callable[[], object], str]] = []
        # 0110 B3: at most one post-gap REST snapshot in flight; a gap that
        # closes meanwhile asks for one more run.
        self._post_gap_snapshot_future: Optional[Future] = None
        self._post_gap_snapshot_running = False
        self._post_gap_snapshot_again = False
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
        """Start all recording components.

        Order: writers, collectors (public, then private), the private
        session row, the initial REST snapshot, the health loop.

        Raises:
            CollectorStartError: A collector did not come up within its
                bound. ``RECORDER_SNAPSHOT_INCOMPLETE`` is logged first (as
                for any other failure while starting the collectors).
        """
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
            # A restart is a new run: drop the previous run's coverage state.
            with self._pending_lock:
                self._pending_futures.clear()
                self._private_write_lost = False
                self._private_write_lost_at = None
            self._private_session_id = None
            self._open_gap_ids = {}
            self._open_gap_start = None
            self._open_gap_failures = 0
            self._pending_gap_writes = []
            self._post_gap_snapshot_future = None
            self._post_gap_snapshot_running = False
            self._post_gap_snapshot_again = False

            logger.info("Starting Recorder...")
            self._start_time = datetime.now(UTC)
            self._event_loop = asyncio.get_running_loop()

            # 0110 B1c-2: collectors first. The run only starts on a connected
            # public stream and a confirmed (authenticated, subscribed) private
            # session; otherwise the launcher sentinel is emitted and the
            # start fails.
            try:
                if not self._config.symbols:
                    raise ValueError("symbols must not be empty")

                # REST client for reconciliation (public endpoints only;
                # empty credentials are intentional — no auth needed).
                rest_client = BybitRestClient(
                    api_key="",
                    api_secret="",
                    testnet=self._config.testnet,
                )
                self._reconciler = GapReconciler(
                    db=self._db,
                    rest_client=rest_client,
                    gap_threshold_seconds=self._config.gap_threshold_seconds,
                )
                await self._init_writers()
                await self._init_collectors()
            except BaseException as e:
                # Any failure here (config, clients, writers, run seeding,
                # collectors) ends the start, a cancellation (shutdown
                # signal) included; every exit path must emit a launcher
                # sentinel (feature 0055).
                logger.error(
                    "Recorder start aborted: %s", str(e) or type(e).__name__
                )
                logger.warning("RECORDER_SNAPSHOT_INCOMPLETE")
                raise

            # 0029 Cross-cutting #4: write the t=0 row of the recording session
            # via REST. Bybit's private streams are event-driven, so a quiet
            # account would otherwise leave the seed-aware replay loader
            # returning NULL for wallet/positions/orders even though state
            # existed live. It runs AFTER the private session is confirmed
            # (0110 B1c-2), so the anchor lies inside the session. The method
            # handles each REST call's failure itself (logged, sentinel
            # INCOMPLETE, the start goes on: the WS stream still gets
            # captured and Phase 4's pre-check refuses to seed from a run
            # missing the initial snapshot). Anything that still escapes it,
            # a cancellation included, emits the sentinel and ends the start.
            if self._config.account:
                try:
                    await self._write_initial_rest_snapshot()
                except BaseException:
                    # Cancelled, or an error escaped, before the snapshot
                    # emitted its own sentinel (that is its last statement).
                    logger.warning("RECORDER_SNAPSHOT_INCOMPLETE")
                    raise

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
        # 0065: subscribe ticker for the traded symbols PLUS any collateral
        # symbols (so non-USDT collateral coins get marks in ticker_snapshots),
        # de-duplicated and order-preserving (traded symbols first). The
        # private collector and initial REST snapshots stay scoped to the
        # traded `symbols` only — collateral coins are not traded.
        public_symbols = list(
            dict.fromkeys(self._config.symbols + self._config.collateral_symbols)
        )
        self._public_collector = PublicCollector(
            symbols=public_symbols,
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
                on_disconnect=self._handle_private_disconnect,
                on_healthy_probe=self._private_checkpoint,
            )
            await self._private_collector.start()
            self._open_private_session(datetime.now(UTC))

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

        Shell sentinel contract (feature 0055):
        When a snapshot is attempted (i.e. ``self._config.account`` is set),
        every exit path of this method MUST emit exactly one of:
        - ``logger.info("RECORDER_SNAPSHOT_OK")`` — wallet_count > 0 AND
          position_count > 0 AND no failed position symbol; replay seed
          will succeed.
        - ``logger.warning("RECORDER_SNAPSHOT_INCOMPLETE")`` — auth-client
          construction failure, zero wallet/position rows, OR a failed
          position symbol (``get_positions`` raised, an open row whose leg
          is unknown, or a real row that failed conversion — 0110 B2a). Positions always write two rows per
          symbol, so ``position_count == 0`` now means no symbols or a
          failed insert; the failed-symbol count is the real
          position-dimension signal. An ``empty_response`` symbol
          (successful fetch, no row for it) deliberately stays OK with
          synthetic flat legs, so replay seeds it flat; B2c decides how to
          treat it (``.claude/rules/recorder.md``). (``start()``
          emits the same sentinel when a collector does not start, in which
          case this method is never reached.)
        ``scripts/phase4/start_recorder.sh`` waits for one of these sentinels
        and dispatches via ``scripts/phase4/lib/recorder_snapshot_check.sh``.
        Adding a new early ``return`` after the account guard without
        emitting a sentinel will hang the shell wait loop until its 60s
        timeout. Never emit both sentinels in a single invocation — the
        classifier treats INCOMPLETE as terminal regardless of any later OK.

        The no-account case (``self._config.account is None``) returns
        without emitting a sentinel — Phase 4's ``start_recorder.sh`` is
        only used with accounts configured (``prepare_recorder_session``
        aborts otherwise), so the wait-loop hang cannot occur in practice.
        """
        if not self._config.account:
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
            logger.error(
                f"Failed to construct authenticated REST client for initial snapshot: {e}. "
                "Check: API key/secret format and pairing, network connectivity to Bybit "
                "(mainnet vs testnet matches config.testnet), and Bybit service status."
            )
            logger.warning("RECORDER_SNAPSHOT_INCOMPLETE")
            return

        run_id_str = str(self._run_id)
        account_id_str = str(self._account_id)
        snapshot_ts = datetime.now(UTC)

        wallet_count = await self._snapshot_wallet(
            auth_client, run_id_str, account_id_str, snapshot_ts
        )
        position_count, position_failures = await self._snapshot_positions(
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
        # legitimately can be zero (clean account) — not warned on. A failed
        # symbol (0110 B2a: get_positions raised, or an open row's leg is
        # unknown) counts as missing: its rows are only placeholders.
        if wallet_count == 0 or position_count == 0 or position_failures:
            logger.warning(
                "Initial REST snapshot incomplete: "
                f"wallet_rows={wallet_count}, position_rows={position_count}, "
                f"position_failed_symbols={position_failures} "
                "(zero on either dimension means seed-aware replay from this "
                "run_id will fail Phase 4 pre-check; check API credentials / "
                "permissions / category=linear settleCoin=USDT scope)"
            )
            logger.warning("RECORDER_SNAPSHOT_INCOMPLETE")
        else:
            logger.info("RECORDER_SNAPSHOT_OK")

    async def _snapshot_wallet(
        self,
        client: BybitRestClient,
        run_id: str,
        account_id: str,
        snapshot_ts: datetime,
        label: str = _INITIAL_SNAPSHOT,
        evidence_only: bool = False,
    ) -> int:
        """REST-fetch wallet balance and write one row per coin. Returns row count.

        With ``evidence_only`` (post-gap snapshot, 0110 B3) nothing is written
        unless the reading carries a USDT ``walletBalance`` and the account's
        ``totalEquity`` and ``totalAvailableBalance``: an empty field is
        coerced to 0 (pitfall 14), the row would become the newest wallet
        seed, and replay refuses to seed from a zero balance.
        """
        try:
            result = await asyncio.to_thread(client.get_wallet_balance, "UNIFIED")
        except Exception as e:
            logger.error(f"{label}: get_wallet_balance failed: {e}")
            return 0

        # Bybit V5 shape: result["list"][0]["coin"] = list of per-coin dicts.
        accounts = result.get("list") or []
        snapshots: list[WalletSnapshot] = []
        proven = False
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
                    f"{label}: skipped malformed wallet account row: {e}"
                )
                continue

            for coin_data in acct.get("coin") or []:
                try:
                    if (
                        coin_data.get("coin") == _WALLET_SEED_COIN
                        and coin_data.get("walletBalance") not in (None, "")
                        and acct.get("totalEquity") not in (None, "")
                        and acct.get("totalAvailableBalance") not in (None, "")
                    ):
                        proven = True
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
                        f"{label}: skipped malformed wallet coin row: {e}"
                    )
                    continue

        if evidence_only and not proven:
            logger.warning(
                f"{label}: wallet reading has no {_WALLET_SEED_COIN} "
                "walletBalance / totalEquity / totalAvailableBalance; earlier "
                "wallet rows stay the seed"
            )
            return 0
        if not snapshots:
            return 0

        try:
            await asyncio.to_thread(self._bulk_insert_wallet_snapshots, snapshots)
        except Exception as e:
            logger.error(f"{label}: wallet bulk_insert failed: {e}")
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
        label: str = _INITIAL_SNAPSHOT,
        evidence_only: bool = False,
    ) -> tuple[int, int]:
        """REST-fetch positions and write BOTH sides per configured symbol.

        Contract: ALWAYS exactly two rows per symbol (Buy + Sell). When the
        REST response omits a side, write a zero-size row for it. A zero-row
        is marked in ``raw_json`` with why it was synthesised (0110 B2a):
        ``{"synthetic": "rest_failure" | "malformed" | "absent_side"}``, so a
        reader can tell a flat leg from a failed or incomplete fetch.

        ``empty_response`` marks both legs of a symbol a successful fetch
        returned no row for (wrong scope, or never traded). A row whose leg
        ``leg_side`` cannot resolve is skipped: if it is flat (one-way mode)
        that is logged at INFO; if it is open, both synthesised legs are
        ``malformed`` and the symbol counts as failed.

        With ``evidence_only`` (post-gap snapshot, 0110 B3) a symbol whose
        fetch failed, came back empty or had a malformed / unresolvable row
        writes NOTHING: after a gap such a placeholder would become replay's
        seed (read as flat) and live-check's unfit end anchor, which is worse
        than keeping the older rows. ``absent_side`` is still written.

        Returns:
            ``(rows_written, failed_symbols)`` — a symbol fails when
            ``get_positions`` raised, an open row's leg is unknown, or a
            real row failed conversion.
        """
        snapshots: list[PositionSnapshot] = []
        failures = 0
        for symbol in self._config.symbols:
            fetch_failed = False
            try:
                positions = await asyncio.to_thread(client.get_positions, symbol)
            except Exception as e:
                logger.error(
                    f"{label}: get_positions({symbol}) failed: {e}"
                )
                # Still write zero-rows for both sides so the loader's
                # "exactly one side missing" check doesn't fire on a
                # transient REST failure.
                positions = []
                fetch_failed = True
                failures += 1

            # Index by side for O(1) lookup.
            by_side: dict[str, dict] = {}
            symbol_rows = 0
            open_row_unresolved = False
            for pos in positions:
                if pos.get("symbol") != symbol:
                    continue
                symbol_rows += 1
                side = leg_side(pos)
                if side in ("Buy", "Sell"):
                    by_side[side] = pos
                    continue
                unresolved = (
                    f"{label}: position row for {symbol} skipped, "
                    f"side could not be resolved (side={pos.get('side')!r}, "
                    f"positionIdx={pos.get('positionIdx')!r}, "
                    f"size={pos.get('size')!r})"
                )
                if _is_flat_position_row(pos):
                    # Expected for a one-way account's empty position.
                    logger.info(unresolved)
                else:
                    logger.warning(unresolved)
                    open_row_unresolved = True

            if fetch_failed:
                synthetic_default = "rest_failure"
            elif open_row_unresolved:
                # An open position exists but its leg is unknown: neither
                # synthesised leg can be claimed flat.
                synthetic_default = "malformed"
                failures += 1
            elif symbol_rows == 0:
                logger.warning(
                    f"{label}: get_positions({symbol}) returned no "
                    "position row (check category=linear settleCoin=USDT "
                    "scope / API-key permissions); legs marked empty_response"
                )
                synthetic_default = "empty_response"
            else:
                synthetic_default = "absent_side"

            row_malformed = False
            symbol_snapshots: list[PositionSnapshot] = []
            for side in ("Buy", "Sell"):
                pos = by_side.get(side)
                synthetic = synthetic_default
                if pos is not None:
                    try:
                        symbol_snapshots.append(
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
                                cur_realised_pnl=(
                                    Decimal(str(pos.get("curRealisedPnl")))
                                    if pos.get("curRealisedPnl") not in (None, "")
                                    else None
                                ),
                                # 0059/0060: Bybit positionValue verbatim (mark-based); not size * entry_price.
                                position_value=(
                                    Decimal(str(pos.get("positionValue")))
                                    if pos.get("positionValue") not in (None, "")
                                    else None
                                ),
                                raw_json=pos,
                            )
                        )
                        continue
                    except Exception as e:
                        logger.warning(
                            f"{label}: malformed position row "
                            f"({symbol} {side}); writing zero-row: {e}"
                        )
                        synthetic = "malformed"
                        row_malformed = True
                # Absent (or malformed): write the contract zero-row.
                symbol_snapshots.append(
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
                        cur_realised_pnl=None,
                        position_value=None,  # 0059: zero-row stays NULL.
                        raw_json={"synthetic": synthetic},
                    )
                )
            # A real row that failed conversion may be an open leg: the
            # symbol fails (counted once with an unresolved open row).
            if row_malformed and not open_row_unresolved:
                failures += 1
            unproven = "malformed" if row_malformed else synthetic_default
            if evidence_only and unproven in _UNPROVEN_SYNTHETIC:
                logger.warning(
                    f"{label}: {symbol} skipped ({unproven}); its earlier "
                    "position rows stay the seed"
                )
                continue
            snapshots.extend(symbol_snapshots)

        if not snapshots:
            return 0, failures

        try:
            await asyncio.to_thread(self._bulk_insert_position_snapshots, snapshots)
        except Exception as e:
            logger.error(f"{label}: position bulk_insert failed: {e}")
            return 0, failures
        return len(snapshots), failures

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
        wall-clock), NOT the order's ``createdTime``. The snapshot row sorts
        BEFORE any WS-stream row that arrives after it for the same
        ``order_id``, so the loader's MAX(exchange_ts) GROUP BY order_id
        picks up a later WS state when one exists. (Since 0110 B1c-2 the
        private stream is subscribed first, so a WS row from the few seconds
        before the snapshot can sort earlier.)
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

        # A post-gap snapshot is best effort: never wait for it on stop.
        if self._post_gap_snapshot_future is not None:
            self._post_gap_snapshot_future.cancel()
            self._post_gap_snapshot_future = None
        self._post_gap_snapshot_running = False

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

    def request_shutdown(self) -> None:
        """Ask :meth:`run_until_shutdown` to stop the recorder.

        Safe to call before ``run_until_shutdown`` is awaiting: the request
        is kept and honoured as soon as it does.
        """
        self._shutdown_event.set()

    async def run_until_shutdown(self) -> None:
        """Run until SIGINT/SIGTERM received."""
        loop = asyncio.get_running_loop()

        def shutdown_handler():
            logger.info("Shutdown signal received")
            self.request_shutdown()

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

    def _submit_private(self, coro, label: str) -> Future:
        """Schedule a private WS write and register it for the checkpoint.

        Creation and registration share ``_pending_lock`` with the barrier in
        :meth:`_private_checkpoint`, so a write submitted before the barrier
        is always awaited by it.
        """
        with self._pending_lock:
            fut = asyncio.run_coroutine_threadsafe(coro, self._event_loop)
            self._pending_futures.add(fut)
        fut.add_done_callback(self._forget_pending)
        fut.add_done_callback(self._log_future_error(label))
        return fut

    def _forget_pending(self, fut: Future) -> None:
        failed = fut.cancelled() or fut.exception() is not None
        with self._pending_lock:
            # Latch inside the lock: the checkpoint barrier copies the
            # registry under it, so it sees either the future or the latch.
            first_loss = failed and not self._private_write_lost
            if failed:
                self._private_write_lost = True
                # Earliest loss not yet recorded as a gap.
                self._private_write_lost_at = (
                    self._private_write_lost_at or datetime.now(UTC)
                )
            self._pending_futures.discard(fut)
        if first_loss:
            # The event never reached a writer: record a gap for it on the
            # loop; the latch blocks the checkpoint until that has run.
            logger.error("Private WS write failed; recording a gap for it")
            loop = self._event_loop
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(self._record_lost_write_gap)
                except RuntimeError:
                    pass  # loop closed: the recorder is stopping

    def _record_lost_write_gap(self) -> None:
        """Turn a lost private write into a gap, then release the latch.

        Runs on the event loop. The gap starts the liveness margin before
        the earliest unrecorded loss (not before "now": this callback can
        run late); REST recovery runs for executions as for any gap. An
        open outage gap covers the loss from its own start, so only the
        part before it (if any) is recorded. If a needed gap cannot be
        recorded (the recorder is stopping) the latch stays set, so the
        checkpoint stays blocked.
        """
        now = datetime.now(UTC)
        with self._pending_lock:
            lost_at = self._private_write_lost_at or now
            # Released before recording: a later loss schedules its own gap.
            self._private_write_lost = False
            self._private_write_lost_at = None
        gap_start = min(lost_at, now) - LIVENESS_MARGIN
        open_start = self._open_gap_start if self._open_gap_ids else None
        if open_start is not None and gap_start >= open_start:
            return  # inside the open outage gap
        if not (self._reconciler and self._config.account and self._run_id):
            with self._pending_lock:  # cannot record: keep the latch
                self._private_write_lost = True
                self._private_write_lost_at = min(
                    self._private_write_lost_at or lost_at, lost_at
                )
            return
        self._recover_private_gap(gap_start, open_start or now, close_open=False)

    def _open_private_session(self, connected_at: datetime) -> None:
        """Write the private-stream session row; its checkpoint starts here.

        Best-effort: without a session no checkpoint is published, so the
        run simply never certifies private coverage.
        """
        if self._run_id is None:
            return
        try:
            with self._db.get_session() as session:
                row = PrivateStreamSessionRepository(session).open_session(
                    run_id=str(self._run_id),
                    account_id=str(self._account_id),
                    connected_at=connected_at,
                )
                session_id = row.id
        except Exception:
            logger.error(
                "Failed to open private stream session; private coverage "
                "will not be certified",
                exc_info=True,
            )
            return
        self._private_session_id = session_id

    async def _private_checkpoint(self) -> None:
        """Advance the coverage checkpoint (collector ``on_healthy_probe``).

        Barrier: retry failed gap writes; take ``barrier_ts`` and copy the
        registered private writes under ``_pending_lock``; await them; flush
        the execution, order, position and wallet writers. Only if all of
        that succeeded, no write was lost and no gap row is open, is
        ``last_checkpoint_ts`` moved to ``barrier_ts - LIVENESS_MARGIN`` (a
        half-open socket can look healthy that long, so a probe cannot
        certify an undetected loss).
        """
        # Always retry, even when nothing can be published below.
        gap_writes_done = self._retry_pending_gap_writes()
        if self._private_session_id is None or self._run_id is None:
            return
        with self._pending_lock:
            barrier_ts = datetime.now(UTC)
            pending = list(self._pending_futures)
        if pending:
            # shield: a cancelled checkpoint (collector timeout / stop) must
            # not cancel the writes themselves.
            results = await asyncio.gather(
                *(asyncio.shield(asyncio.wrap_future(f)) for f in pending),
                return_exceptions=True,
            )
            if any(isinstance(r, BaseException) for r in results):
                return  # _forget_pending records a gap for the lost write
        if self._private_write_lost or not gap_writes_done:
            return
        if self._open_gap_ids:
            return  # an outage is open: nothing to certify until it closes
        for writer in (
            self._execution_writer,
            self._order_writer,
            self._position_writer,
            self._wallet_writer,
        ):
            if writer is not None and not await writer.flush():
                return  # the writer logged the DB error; retry next probe
        if self._pending_gap_writes:
            return  # an outcome write failed while we were awaiting
        with self._db.get_session() as session:
            PrivateStreamSessionRepository(session).advance_checkpoint(
                self._private_session_id,
                run_id=str(self._run_id),
                ts=barrier_ts - LIVENESS_MARGIN,
            )

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
            return self._submit_private(
                self._execution_writer.write([event]), "execution write"
            )
        return None

    def _handle_order(self, account_id: UUID, event: OrderUpdateEvent) -> Optional[Future]:
        """Route order event to writer."""
        if self._order_writer and self._event_loop:
            return self._submit_private(
                self._order_writer.write(account_id, [event]), "order write"
            )
        return None

    def _handle_position(self, account_id: UUID, message: dict) -> Optional[Future]:
        """Route position snapshot to writer."""
        if self._position_writer and self._event_loop:
            return self._submit_private(
                self._position_writer.write(account_id, [message]), "position write"
            )
        return None

    def _handle_wallet(self, account_id: UUID, message: dict) -> Optional[Future]:
        """Route wallet snapshot to writer."""
        if self._wallet_writer and self._event_loop:
            return self._submit_private(
                self._wallet_writer.write(account_id, [message]), "wallet write"
            )
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

    def _handle_private_disconnect(self, gap_start: datetime) -> None:
        """Open one gap row per symbol before the collector resets the socket.

        One transaction for all symbols; the ids are kept only after it
        commits. A symbol that already holds an open row is skipped (the
        collector calls this again on every probe until the gap closes).
        DB errors propagate, so the collector skips the reset this probe —
        for at most ``_MAX_OPEN_GAP_FAILURES - 1`` (2) probes in a row; on
        the next failure it returns normally, the reset goes ahead and the
        gap is recorded at reconnect instead.
        """
        if not (self._config.account and self._run_id):
            return
        # dict.fromkeys: a symbol listed twice in the config gets one row.
        missing = [
            s
            for s in dict.fromkeys(self._config.symbols)
            if s not in self._open_gap_ids
        ]
        if not missing:
            return
        try:
            with self._db.get_session() as session:
                repo = PrivateStreamGapRepository(session)
                opened = {
                    symbol: repo.add_gap(
                        run_id=str(self._run_id),
                        account_id=str(self._account_id),
                        symbol=symbol,
                        gap_start=gap_start,
                        gap_end=None,
                    ).id
                    for symbol in missing
                }
        except Exception:
            self._open_gap_failures += 1
            if self._open_gap_failures < _MAX_OPEN_GAP_FAILURES:
                raise
            if self._open_gap_failures == _MAX_OPEN_GAP_FAILURES:
                logger.error(
                    "Could not open private gap rows in %d probes; letting "
                    "the socket reset anyway (the gap is recorded at "
                    "reconnect)",
                    self._open_gap_failures,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "Still cannot open private gap rows (%d failures in a row)",
                    self._open_gap_failures,
                )
            return
        self._open_gap_failures = 0
        if not self._open_gap_ids:
            self._open_gap_start = gap_start
        self._open_gap_ids.update(opened)

    def _handle_private_gap(
        self, gap_start: datetime, gap_end: datetime
    ) -> list[Future]:
        """Collector callback: the outage ``[gap_start, gap_end]`` is over.

        Closes the open gap rows and reconciles via REST.
        """
        self._open_gap_failures = 0  # this outage is over
        return self._recover_private_gap(gap_start, gap_end, close_open=True)

    def _recover_private_gap(
        self, gap_start: datetime, gap_end: datetime, *, close_open: bool
    ) -> list[Future]:
        """Record a private stream gap and reconcile it via REST API.

        With ``close_open`` each symbol's open gap row is closed (a symbol
        with none - late readiness on the same socket - gets a closed row).
        Without it the open rows are left alone and a separate closed row is
        recorded (a lost write, not the end of an outage). Each symbol's
        recovery outcome is stored when its future completes.
        """
        # Count unconditionally (see _handle_public_gap comment).
        with self._gap_lock:
            self._gap_count += 1
        gap_seconds = (gap_end - gap_start).total_seconds()
        logger.warning(
            f"Private stream gap detected: {gap_seconds:.1f}s "
            f"({gap_start} to {gap_end})"
        )

        self._retry_pending_gap_writes()
        futures: list[Future] = []
        if (
            self._reconciler
            and self._event_loop
            and self._config.account
            and self._run_id
        ):
            for symbol in dict.fromkeys(self._config.symbols):
                # Forget the open id now, even if the close write fails: a
                # queued close must only ever touch this outage's row, and
                # the next outage must open its own.
                gap_id = (
                    self._open_gap_ids.pop(symbol, None) if close_open else None
                )
                if gap_id is not None:
                    self._try_gap_write(
                        self._close_gap_write(gap_id, gap_end),
                        f"gap close for {symbol} (gap {gap_id})",
                    )
                else:
                    gap_id = self._record_private_gap(symbol, gap_start, gap_end)
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
                if gap_id is not None:
                    fut.add_done_callback(self._persist_gap_outcome(gap_id, symbol))
                futures.append(fut)
            self._schedule_post_gap_snapshot()
        if not self._open_gap_ids:
            self._open_gap_start = None
        return futures

    def _schedule_post_gap_snapshot(self) -> None:
        """Re-snapshot wallet + positions over REST after a gap (0110 B3).

        Replay seeds from the latest rows at a window's start, and live-check
        requires gap-free coverage from those rows on. A leg whose size has
        not changed keeps an old seed row, so without a fresh snapshot every
        later window would reach back over the gap and SKIP. At most one
        snapshot is in flight; a gap closing meanwhile asks for one more run,
        so the last snapshot always postdates the last gap.

        Runs on the event loop (gap paths are loop callbacks). "In flight"
        is ``_post_gap_snapshot_running``, cleared by the coroutine itself in
        the same step as its last rerun check — not the future's ``done()``,
        which a later loop callback sets, so a gap in between would be lost.
        """
        if self._post_gap_snapshot_running:
            self._post_gap_snapshot_again = True
            return
        self._post_gap_snapshot_running = True
        self._post_gap_snapshot_future = asyncio.run_coroutine_threadsafe(
            self._post_gap_snapshots(), self._event_loop
        )
        self._post_gap_snapshot_future.add_done_callback(
            self._log_post_gap_snapshot_error
        )

    @staticmethod
    def _log_post_gap_snapshot_error(future: Future) -> None:
        if not future.cancelled() and (exc := future.exception()) is not None:
            logger.error("Post-gap snapshot failed: %s", exc)

    async def _post_gap_snapshots(self) -> None:
        try:
            while self._running:
                self._post_gap_snapshot_again = False
                await self._write_post_gap_snapshot()
                if not self._post_gap_snapshot_again:
                    return
        finally:
            self._post_gap_snapshot_running = False

    async def _write_post_gap_snapshot(self) -> None:
        """One REST snapshot of wallet + positions, evidence only.

        No ``RECORDER_SNAPSHOT_*`` sentinel (those are the launcher's startup
        contract), no position placeholders (see ``_snapshot_positions``), and
        the wallet only when every symbol's positions landed.
        """
        try:
            client = BybitRestClient(
                api_key=self._config.account.api_key.get_secret_value(),
                api_secret=self._config.account.api_secret.get_secret_value(),
                testnet=self._config.testnet,
            )
        except Exception as e:
            logger.error(f"{_POST_GAP_SNAPSHOT}: REST client failed: {e}")
            return
        run_id = str(self._run_id)
        account_id = str(self._account_id)
        snapshot_ts = datetime.now(UTC)
        position_count, failed_symbols = await self._snapshot_positions(
            client, run_id, account_id, snapshot_ts,
            label=_POST_GAP_SNAPSHOT, evidence_only=True,
        )
        # The wallet seed moves past the gap only together with the
        # positions: a post-gap wallet paired with pre-gap positions would be
        # an inconsistent replay seed.
        wallet_count = 0
        if position_count and not failed_symbols:
            wallet_count = await self._snapshot_wallet(
                client, run_id, account_id, snapshot_ts,
                label=_POST_GAP_SNAPSHOT, evidence_only=True,
            )
        if position_count or wallet_count:
            logger.info(
                f"{_POST_GAP_SNAPSHOT}: wallet={wallet_count} coins, "
                f"positions={position_count} rows"
            )
        else:
            logger.warning(
                f"{_POST_GAP_SNAPSHOT}: wrote nothing; later live-check windows "
                "SKIP until a leg's size changes or the recorder restarts"
            )

    def _record_private_gap(
        self, symbol: str, gap_start: datetime, gap_end: datetime
    ) -> Optional[int]:
        """Persist an already-closed gap row; its id, or None on a DB error.

        Fallback for a symbol with no open row (late readiness: the socket
        was never reset, so no row was opened at disconnect). On a DB error
        recovery still runs — the backfill matters more than its bookkeeping
        row — and the insert is queued, so the checkpoint waits for the row
        (its outcome then stays ``pending``).
        """
        run_id = str(self._run_id)
        account_id = str(self._account_id)

        def _add() -> int:
            with self._db.get_session() as session:
                return PrivateStreamGapRepository(session).add_gap(
                    run_id=run_id,
                    account_id=account_id,
                    symbol=symbol,
                    gap_start=gap_start,
                    gap_end=gap_end,
                ).id

        try:
            return _add()
        except Exception:
            logger.error(
                "Failed to record private stream gap for %s; retrying before "
                "the next checkpoint", symbol,
                exc_info=True,
            )
            self._pending_gap_writes.append((_add, f"gap row for {symbol}"))
            return None

    def _persist_gap_outcome(
        self, gap_id: int, symbol: str
    ) -> Callable[[Future], None]:
        """Return a done-callback that stores a gap's recovery outcome."""
        run_id = str(self._run_id)  # the run the gap row was written under

        def _cb(future: Future) -> None:
            result = recovery_result_from_future(future)

            def _write() -> None:
                with self._db.get_session() as session:
                    PrivateStreamGapRepository(session).set_outcome(
                        gap_id,
                        run_id=run_id,
                        status=result.status,
                        inserted=result.inserted,
                        duplicates=result.duplicates,
                        reason=result.reason,
                    )

            self._try_gap_write(_write, f"gap outcome for {symbol} (gap {gap_id})")
        return _cb

    def _close_gap_write(
        self, gap_id: int, gap_end: datetime
    ) -> Callable[[], None]:
        """A write that closes the gap row ``gap_id`` at ``gap_end``."""
        run_id = str(self._run_id)

        def _write() -> None:
            with self._db.get_session() as session:
                PrivateStreamGapRepository(session).close_gap(
                    gap_id, run_id=run_id, gap_end=gap_end
                )
        return _write

    def _try_gap_write(
        self, write: Callable[[], object], label: str, *, retry: bool = False
    ) -> None:
        """Run a gap bookkeeping write; queue it for retry on a DB error.

        Queued writes are retried by every checkpoint attempt and every
        private gap; the checkpoint does not advance until the queue is
        empty. A missing row (``RowNotFoundError``) cannot be fixed by
        retrying and is dropped. A retry that fails again logs one line, no traceback.
        """
        try:
            write()
        except RowNotFoundError:
            logger.error("Failed to persist %s: row not found", label, exc_info=True)
        except Exception as exc:
            if retry:
                logger.warning("Still failing to persist %s: %s", label, exc)
            else:
                logger.error(
                    "Failed to persist %s; retrying before the next checkpoint",
                    label,
                    exc_info=True,
                )
            self._pending_gap_writes.append((write, label))

    def _retry_pending_gap_writes(self) -> bool:
        """Retry queued gap writes; True when none remain."""
        writes, self._pending_gap_writes = self._pending_gap_writes, []
        for write, label in writes:
            self._try_gap_write(write, label, retry=True)
        return not self._pending_gap_writes

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

        # 0110 B1c-1: kept on liveness checks only = coverage not certified.
        if self._private_collector:
            stats["private_ws"] = {
                "degraded": self._private_collector.is_degraded()
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
