"""Tests for the recorder's initial REST snapshot (0029 Cross-cutting #4).

The recorder writes a t=0 row of wallet/positions/open-orders to the DB so the
seed-aware replay loader always finds at least one row per dimension, even on
quiet accounts where Bybit's event-driven private streams emit no updates for
many minutes after connect.

Contract (from docs/features/0029_PLAN.md "Initial-snapshot row contract"):
- Wallet: one row per coin returned by ``get_wallet_balance``.
- Positions: ALWAYS two rows per configured symbol (Buy + Sell), even when the
  REST response omits a side (write a zero-size row in that case).
- Orders: one row per open order with reduce_only / order_link_id from the
  response and ``exchange_ts``/``local_ts`` set to the snapshot wall-clock.
- REST failures must NOT abort recorder start.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grid_db import Order, PositionSnapshot, WalletSnapshot
from recorder.recorder import Recorder


def _make_pub_mock():
    mock_pub = MagicMock()
    mock_pub.start = AsyncMock()
    mock_pub.stop = AsyncMock()
    mock_pub.get_connection_state.return_value = None
    return mock_pub


def _make_priv_mock():
    mock_priv = MagicMock()
    mock_priv.start = AsyncMock()
    mock_priv.stop = AsyncMock()
    return mock_priv


def _stub_rest_client(
    *,
    wallet_response: dict,
    positions_by_symbol: dict[str, list[dict]],
    open_orders_by_symbol: dict[str, list[dict]],
):
    """Return a mock BybitRestClient configured with canned responses.

    ``positions_by_symbol`` and ``open_orders_by_symbol`` are keyed by the
    symbol passed to ``get_positions(symbol)`` / ``get_open_orders(symbol, ...)``.
    """
    mock = MagicMock()
    mock.get_wallet_balance.return_value = wallet_response
    mock.get_positions.side_effect = lambda symbol: positions_by_symbol.get(
        symbol, []
    )
    mock.get_open_orders.side_effect = lambda symbol, order_type="Limit": (
        open_orders_by_symbol.get(symbol, [])
    )
    return mock


class TestInitialRestSnapshot:
    """End-to-end tests for ``_write_initial_rest_snapshot``."""

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_writes_two_rows_per_symbol_even_when_no_position(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        """REST returns only a Buy row → recorder writes 2 rows (Buy + Sell @ size=0)."""
        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        # Snapshot client returns one Buy position; the contract requires the
        # recorder to ALSO write a zero-size Sell row to keep the loader's
        # "exactly one side missing" invariant unambiguous.
        snapshot_client = _stub_rest_client(
            wallet_response={"list": []},
            positions_by_symbol={
                "BTCUSDT": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "size": "0.5",
                        "entryPrice": "50000.0",
                        "liqPrice": "45000.0",
                        "unrealisedPnl": "100.0",
                    }
                ],
            },
            open_orders_by_symbol={"BTCUSDT": []},
        )
        # First BybitRestClient(...) call is the public reconciler client
        # (empty creds); the second is the authenticated snapshot client.
        # MagicMock() instance for the reconciler is fine — it's never called.
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        with db.get_session() as session:
            rows = (
                session.query(PositionSnapshot)
                .filter(PositionSnapshot.run_id == str(recorder._run_id))
                .filter(PositionSnapshot.symbol == "BTCUSDT")
                .all()
            )
            assert len(rows) == 2, (
                f"Expected exactly 2 position rows (one Buy, one Sell); got {len(rows)}"
            )
            sides = {r.side for r in rows}
            assert sides == {"Buy", "Sell"}

            buy_row = next(r for r in rows if r.side == "Buy")
            sell_row = next(r for r in rows if r.side == "Sell")

            assert buy_row.size == pytest.approx(buy_row.size.__class__("0.5"))
            assert sell_row.size == sell_row.size.__class__("0")
            assert sell_row.entry_price == sell_row.entry_price.__class__("0")
            assert sell_row.liq_price is None

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_wallet_writes_per_coin(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        """Wallet response with USDT + BTC → 2 wallet rows."""
        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        snapshot_client = _stub_rest_client(
            wallet_response={
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "marginMode": "REGULAR_MARGIN",
                        "totalEquity": "15000.50",
                        "totalAvailableBalance": "14000.25",
                        "totalMarginBalance": "14900.75",
                        "accountIMRate": "0.01000000",
                        "accountMMRate": "0.00500000",
                        "coin": [
                            {
                                "coin": "USDT",
                                "walletBalance": "10000.5",
                                "availableToWithdraw": "9500.25",
                            },
                            {
                                "coin": "BTC",
                                "walletBalance": "0.25",
                                "availableToWithdraw": "0.20",
                            },
                        ]
                    }
                ]
            },
            positions_by_symbol={"BTCUSDT": []},
            open_orders_by_symbol={"BTCUSDT": []},
        )
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        with db.get_session() as session:
            rows = (
                session.query(WalletSnapshot)
                .filter(WalletSnapshot.run_id == str(recorder._run_id))
                .all()
            )
            assert len(rows) == 2, f"Expected 2 wallet rows (one per coin); got {len(rows)}"
            coins = {r.coin for r in rows}
            assert coins == {"USDT", "BTC"}

            usdt_row = next(r for r in rows if r.coin == "USDT")
            assert usdt_row.wallet_balance == usdt_row.wallet_balance.__class__("10000.5")
            assert usdt_row.available_balance == usdt_row.available_balance.__class__("9500.25")
            assert usdt_row.total_equity == usdt_row.total_equity.__class__("15000.50")
            assert usdt_row.total_available_balance == usdt_row.total_available_balance.__class__("14000.25")
            assert usdt_row.total_margin_balance == usdt_row.total_margin_balance.__class__("14900.75")
            assert usdt_row.account_im_rate == usdt_row.account_im_rate.__class__("0.01000000")
            assert usdt_row.account_mm_rate == usdt_row.account_mm_rate.__class__("0.00500000")
            assert usdt_row.raw_json["_account"] == {
                "accountType": "UNIFIED",
                "marginMode": "REGULAR_MARGIN",
                "totalEquity": "15000.50",
                "totalAvailableBalance": "14000.25",
                "totalMarginBalance": "14900.75",
                "accountIMRate": "0.01000000",
                "accountMMRate": "0.00500000",
            }

            btc_row = next(r for r in rows if r.coin == "BTC")
            assert btc_row.total_available_balance == usdt_row.total_available_balance

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_open_orders_capture_reduce_only(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        """Open order with reduceOnly=True → row.reduce_only is True."""
        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        snapshot_client = _stub_rest_client(
            wallet_response={"list": []},
            positions_by_symbol={"BTCUSDT": []},
            open_orders_by_symbol={
                "BTCUSDT": [
                    {
                        "orderId": "exchange-id-123",
                        "orderLinkId": "client-id-abc",
                        "symbol": "BTCUSDT",
                        "side": "Sell",
                        "price": "55000.0",
                        "qty": "0.1",
                        "cumExecQty": "0",
                        "leavesQty": "0.1",
                        "reduceOnly": True,
                        "orderType": "Limit",
                    }
                ]
            },
        )
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        with db.get_session() as session:
            rows = (
                session.query(Order)
                .filter(Order.run_id == str(recorder._run_id))
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.reduce_only is True
            assert row.order_link_id == "client-id-abc"
            assert row.order_id == "exchange-id-123"
            assert row.status == "New"
            assert row.leaves_qty == row.leaves_qty.__class__("0.1")

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_rest_failure_does_not_abort_recorder(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls, config_with_account, db, db_with_gridbot_seed
    ):
        """All REST calls raise → recorder.start() still completes; private collector started."""
        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv = _make_priv_mock()
        mock_priv_cls.return_value = mock_priv

        snapshot_client = MagicMock()
        snapshot_client.get_wallet_balance.side_effect = RuntimeError("REST down")
        snapshot_client.get_positions.side_effect = RuntimeError("REST down")
        snapshot_client.get_open_orders.side_effect = RuntimeError("REST down")
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()  # must not raise

        # Private collector must still have been created and started.
        mock_priv_cls.assert_called_once()
        mock_priv.start.assert_awaited_once()
        assert recorder._private_collector is not None
        assert recorder._running is True

        # Even on full REST failure, the contract still writes the
        # zero-row position pair so loader semantics stay binary
        # (both-empty handled by Phase 4 pre-check, exactly-one-side-missing
        # treated as corrupt).
        with db.get_session() as session:
            pos_rows = (
                session.query(PositionSnapshot)
                .filter(PositionSnapshot.run_id == str(recorder._run_id))
                .all()
            )
            assert len(pos_rows) == 2
            assert {r.side for r in pos_rows} == {"Buy", "Sell"}
            assert all(r.size == r.size.__class__("0") for r in pos_rows)

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_warning_emitted_when_wallet_snapshot_empty(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed, caplog,
    ):
        """When wallet REST returns empty (e.g. credential issue), an explicit
        WARNING is logged so operator catches the seeding-blocker at recorder
        start instead of finding out hours later when replay's pre-check fails.
        Open orders being zero is NOT warned on (clean account is valid).
        """
        import logging

        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv = _make_priv_mock()
        mock_priv_cls.return_value = mock_priv

        # Wallet response empty → wallet_count == 0 → WARNING expected.
        # Position response has one Buy row for the configured symbol; the
        # contract still writes a zero Sell row, so position_count == 2 > 0.
        # Open orders empty — must NOT trip the warning by itself.
        snapshot_client = _stub_rest_client(
            wallet_response={"list": []},
            positions_by_symbol={
                "BTCUSDT": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "size": "0.5",
                        "entryPrice": "50000",
                        "liqPrice": "45000",
                        "unrealisedPnl": "0",
                    }
                ]
            },
            open_orders_by_symbol={"BTCUSDT": []},
        )
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        with caplog.at_level(logging.WARNING, logger="recorder.recorder"):
            await recorder.start()

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "Initial REST snapshot incomplete" in r.message
        ]
        assert len(warnings) == 1, (
            f"expected exactly one incomplete-snapshot warning, "
            f"got {len(warnings)}: {[r.message for r in warnings]}"
        )
        assert "wallet_rows=0" in warnings[0].message
        # position_rows>0 in this scenario; spot-check that the message
        # surfaces the actual count so the warning is actionable.
        assert "position_rows=" in warnings[0].message

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_sentinel_ok_on_success(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed, caplog,
    ):
        """0055: success path emits RECORDER_SNAPSHOT_OK (shell sentinel).

        Used by scripts/phase4/lib/recorder_snapshot_check.sh to distinguish
        success from incomplete (zero counts) and timeout.
        """
        import logging

        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        snapshot_client = _stub_rest_client(
            wallet_response={
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "coin": [
                            {"coin": "USDT", "walletBalance": "10000", "availableToWithdraw": "9000"},
                        ],
                    }
                ]
            },
            positions_by_symbol={
                "BTCUSDT": [
                    {
                        "symbol": "BTCUSDT", "side": "Buy", "size": "0.5",
                        "entryPrice": "50000", "liqPrice": "45000", "unrealisedPnl": "0",
                    }
                ]
            },
            open_orders_by_symbol={"BTCUSDT": []},
        )
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        with caplog.at_level(logging.INFO, logger="recorder.recorder"):
            await recorder.start()

        ok = [r for r in caplog.records if r.message == "RECORDER_SNAPSHOT_OK"]
        incomplete = [r for r in caplog.records if r.message == "RECORDER_SNAPSHOT_INCOMPLETE"]
        assert len(ok) == 1, f"expected RECORDER_SNAPSHOT_OK once; got {len(ok)}"
        assert incomplete == [], f"unexpected INCOMPLETE on success path: {incomplete}"

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_sentinel_incomplete_on_zero_counts(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed, caplog,
    ):
        """0055: zero-count failure path emits RECORDER_SNAPSHOT_INCOMPLETE."""
        import logging

        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        snapshot_client = _stub_rest_client(
            wallet_response={"list": []},
            positions_by_symbol={"BTCUSDT": []},
            open_orders_by_symbol={"BTCUSDT": []},
        )
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        with caplog.at_level(logging.WARNING, logger="recorder.recorder"):
            await recorder.start()

        incomplete = [r for r in caplog.records if r.message == "RECORDER_SNAPSHOT_INCOMPLETE"]
        ok = [r for r in caplog.records if r.message == "RECORDER_SNAPSHOT_OK"]
        assert len(incomplete) == 1, (
            f"expected RECORDER_SNAPSHOT_INCOMPLETE once; got {len(incomplete)}"
        )
        assert ok == [], f"unexpected OK on incomplete path: {ok}"

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_sentinel_incomplete_on_auth_client_failure(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed, caplog,
    ):
        """0055: BybitRestClient construction raises → INCOMPLETE sentinel emitted
        without reaching the count-based branch (no INFO 'Initial REST snapshot:').
        """
        import logging

        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        # First call (public reconciler) succeeds with a plain MagicMock; second
        # call (authenticated snapshot client) raises.
        mock_rest_cls.side_effect = [MagicMock(), RuntimeError("bad credentials")]

        recorder = Recorder(config=config_with_account, db=db)
        with caplog.at_level(logging.WARNING, logger="recorder.recorder"):
            await recorder.start()

        incomplete = [r for r in caplog.records if r.message == "RECORDER_SNAPSHOT_INCOMPLETE"]
        assert len(incomplete) == 1, (
            f"expected RECORDER_SNAPSHOT_INCOMPLETE once on auth failure; "
            f"got {len(incomplete)}"
        )
        # Must NOT have reached the count-based logging branch.
        snapshot_info = [
            r for r in caplog.records
            if r.message.startswith("Initial REST snapshot:")
        ]
        assert snapshot_info == [], (
            f"auth failure must short-circuit before count INFO line; "
            f"got {[r.message for r in snapshot_info]}"
        )

        await recorder.stop()

    # 0056: REST snapshot ``curRealisedPnl`` parsing into ``cur_realised_pnl``.

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_cur_realised_pnl_parsed_from_rest_payload(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed,
    ):
        """0056: REST ``curRealisedPnl`` populates the new column for both sides."""
        from decimal import Decimal

        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        snapshot_client = _stub_rest_client(
            wallet_response={"list": []},
            positions_by_symbol={
                "BTCUSDT": [
                    {
                        "symbol": "BTCUSDT", "side": "Buy", "size": "0.5",
                        "entryPrice": "50000", "liqPrice": "45000",
                        "unrealisedPnl": "0", "curRealisedPnl": "12.5",
                    },
                    {
                        "symbol": "BTCUSDT", "side": "Sell", "size": "0.25",
                        "entryPrice": "51000", "liqPrice": "55000",
                        "unrealisedPnl": "0", "curRealisedPnl": "-3.75",
                    },
                ]
            },
            open_orders_by_symbol={"BTCUSDT": []},
        )
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        with db.get_session() as session:
            rows = (
                session.query(PositionSnapshot)
                .filter(PositionSnapshot.run_id == str(recorder._run_id))
                .filter(PositionSnapshot.symbol == "BTCUSDT")
                .all()
            )
            by_side = {r.side: r for r in rows}
            assert by_side["Buy"].cur_realised_pnl == Decimal("12.5")
            assert by_side["Sell"].cur_realised_pnl == Decimal("-3.75")

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_cur_realised_pnl_missing_yields_none(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed,
    ):
        """0056: REST payload without curRealisedPnl writes NULL (not 0)."""
        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        snapshot_client = _stub_rest_client(
            wallet_response={"list": []},
            positions_by_symbol={
                "BTCUSDT": [
                    {
                        "symbol": "BTCUSDT", "side": "Buy", "size": "0.5",
                        "entryPrice": "50000", "liqPrice": "45000",
                        "unrealisedPnl": "0",
                    }
                ]
            },
            open_orders_by_symbol={"BTCUSDT": []},
        )
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        with db.get_session() as session:
            buy_row = (
                session.query(PositionSnapshot)
                .filter(PositionSnapshot.run_id == str(recorder._run_id))
                .filter(PositionSnapshot.symbol == "BTCUSDT")
                .filter(PositionSnapshot.side == "Buy")
                .one()
            )
            assert buy_row.cur_realised_pnl is None

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_cur_realised_pnl_preserves_explicit_zero(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed,
    ):
        """0056: ``"0"`` curRealisedPnl is preserved as Decimal("0"), not NULL.

        Bybit emits ``"0"`` immediately after a position open before any
        cycle-realized PnL exists. Distinguishing zero from missing matters
        for parity recomputation.
        """
        from decimal import Decimal

        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        snapshot_client = _stub_rest_client(
            wallet_response={"list": []},
            positions_by_symbol={
                "BTCUSDT": [
                    {
                        "symbol": "BTCUSDT", "side": "Buy", "size": "0.5",
                        "entryPrice": "50000", "liqPrice": "45000",
                        "unrealisedPnl": "0", "curRealisedPnl": "0",
                    }
                ]
            },
            open_orders_by_symbol={"BTCUSDT": []},
        )
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        with db.get_session() as session:
            buy_row = (
                session.query(PositionSnapshot)
                .filter(PositionSnapshot.run_id == str(recorder._run_id))
                .filter(PositionSnapshot.symbol == "BTCUSDT")
                .filter(PositionSnapshot.side == "Buy")
                .one()
            )
            assert buy_row.cur_realised_pnl == Decimal("0")

        await recorder.stop()

    @patch("recorder.recorder.PrivateCollector")
    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_zero_row_placeholder_has_null_cur_realised_pnl(
        self, mock_rest_cls, mock_pub_cls, mock_priv_cls,
        config_with_account, db, db_with_gridbot_seed,
    ):
        """0056: when REST omits a side, the synthetic zero-row stores NULL."""
        mock_pub_cls.return_value = _make_pub_mock()
        mock_priv_cls.return_value = _make_priv_mock()

        # REST returns only a Buy row; recorder synthesizes a zero Sell row.
        snapshot_client = _stub_rest_client(
            wallet_response={"list": []},
            positions_by_symbol={
                "BTCUSDT": [
                    {
                        "symbol": "BTCUSDT", "side": "Buy", "size": "0.5",
                        "entryPrice": "50000", "liqPrice": "45000",
                        "unrealisedPnl": "0", "curRealisedPnl": "12.5",
                    }
                ]
            },
            open_orders_by_symbol={"BTCUSDT": []},
        )
        mock_rest_cls.side_effect = [MagicMock(), snapshot_client]

        recorder = Recorder(config=config_with_account, db=db)
        await recorder.start()

        with db.get_session() as session:
            sell_row = (
                session.query(PositionSnapshot)
                .filter(PositionSnapshot.run_id == str(recorder._run_id))
                .filter(PositionSnapshot.symbol == "BTCUSDT")
                .filter(PositionSnapshot.side == "Sell")
                .one()
            )
            assert sell_row.size == sell_row.size.__class__("0")
            assert sell_row.cur_realised_pnl is None

        await recorder.stop()
