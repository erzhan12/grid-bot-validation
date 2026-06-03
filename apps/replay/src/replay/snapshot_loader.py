"""Seed-aware replay snapshot loader (feature 0029, Phase 2A).

Pure functions that translate recorder-DB rows + ``GridStateStore`` JSON
into plain seed dataclasses ready for the replay engine to inject into a
``BacktestRunner``. The loader owns no DB session and no exchange I/O — it
just adapts the persistence layer to the seed contract documented in
``docs/features/0029_PLAN.md``.

Seed loaders, one per seed dimension:

* :func:`load_grid_state` — wraps :class:`GridStateStore` and returns
  ``None`` on absence / legacy format / step-or-count mismatch. The
  caller decides what to do: under ``seed.enabled=True`` the engine
  raises :class:`SeedDataQualityError` (see ``engine._load_seed``);
  under ``seed.enabled=False`` replay still fresh-builds.
* :func:`load_position_snapshots` — always returns a ``(long, short)``
  pair. Both-absent maps to ``(zero, zero)`` (caught upstream by Phase 4
  pre-check); one-side-only is a corrupt run and raises
  :class:`SeedDataQualityError`.
* :func:`load_wallet_seed_full` — returns UTA account-level wallet seed fields
  for feature 0042 liquidation parity.
* :func:`load_wallet_snapshot` — legacy per-coin helper, kept indefinitely for
  callers that only need ``wallet_balance``.
* :func:`load_active_orders` — ``[]`` is a valid clean-account result;
  per-row ``reduce_only IS NULL`` raises :class:`SeedSchemaError`.

Direction derivation for active orders follows Bybit hedge-mode:

============================  =========
side, reduce_only             direction
============================  =========
``Buy``,  ``False``           ``long``
``Sell``, ``False``           ``short``
``Buy``,  ``True``            ``short`` (closing short by buying)
``Sell``, ``True``            ``long``  (closing long by selling)
============================  =========
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from grid_db import (
    GridStateSnapshot,
    GridStateSnapshotRepository,
    OrderRepository,
    PositionSnapshotRepository,
    TickerSnapshotRepository,
    WalletSnapshotRepository,
)

from comparator.position_loader import _probe_schema as _probe_position_schema

from gridcore.intents import extract_client_order_prefix
from gridcore.persistence import GridStateStore


logger = logging.getLogger(__name__)


def _strip_tz(dt: datetime) -> datetime:
    """Drop tzinfo for cross-source datetime arithmetic.

    Mirrors ``engine._seed_pre_check`` (engine.py): SQLite hands back naive
    ``exchange_ts`` while config ``at_ts`` is tz-aware, so subtracting them
    directly raises ``TypeError``. Normalise both sides before comparing.
    """
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


# ---------------------------------------------------------------------------
# Seed dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GridStateSeed:
    """Saved grid state for replay restoration.

    ``grid`` is the full ordered list of ``{'side': ..., 'price': ...}``
    dicts as persisted by :meth:`GridStateStore.save`. ``anchor_price`` is
    intentionally NOT stored — live drops it post-feature-0021 and replay
    reconstructs it via ``Grid.wait_center()`` after ``restore_grid``.
    """

    strat_id: str
    grid: list[dict]
    grid_step: float
    grid_count: int


@dataclass(frozen=True)
class PositionStateSeed:
    """Per-direction seed for ``BacktestPositionTracker.seed_state``.

    ``direction`` is ``'long'`` or ``'short'`` (lowercase, matching
    ``DirectionType`` string values). ``leverage`` is NOT seeded —
    ``PositionSnapshot`` does not store it; replay reads leverage from
    its strategy config at tracker init time.

    ``cum_realised_pnl`` (0034) is the Bybit ``cumRealisedPnl`` value at
    ``at_ts`` — the cumulative realized PnL since position open. Seeded
    into the tracker so backtest ``cum_realised_pnl`` parity is measured
    against the same absolute baseline as live, not against a zero-start.
    Defaults to ``Decimal('0')`` for snapshots with ``NULL`` telemetry or
    pre-0034 recorder DBs.
    """

    direction: str
    size: Decimal
    entry_price: Decimal
    liquidation_price: Decimal
    cum_realised_pnl: Decimal = Decimal("0")
    cur_realised_pnl: Decimal = Decimal("0")


@dataclass(frozen=True)
class WalletSeed:
    """Wallet seed with both legacy per-coin and UTA account-level fields.

    ``coin_balance`` is the legacy per-coin ``coin[].walletBalance`` for the
    requested coin. ``total_available_balance`` is Bybit's account-level UTA
    available balance in USD and is the value replay now feeds into
    ``BacktestSession.initial_balance`` for liquidation-price parity.
    Account IM/MM rates are raw Bybit decimal ratios, not percentages.
    """

    coin_balance: Decimal
    total_available_balance: Decimal
    total_equity: Decimal
    total_margin_balance: Decimal
    account_im_rate: Decimal
    account_mm_rate: Decimal

    # --- Feature 0065: non-USDT collateral re-marking ---
    # All default to empty so existing constructors / tests stay valid.
    # Frozen dataclass: mutable defaults MUST use ``field(default_factory=...)``
    # (a bare ``= {}`` raises at class-definition time), and the loader builds a
    # NEW ``WalletSeed`` (or ``dataclasses.replace``) rather than mutating.
    coin_balances: dict[str, Decimal] = field(default_factory=dict)
    """Per-coin wallet balance for each MODELLED collateral coin (excludes the
    USDT/``wallet_coin`` row, which is ``coin_balance``)."""
    seed_marks: dict[str, Decimal] = field(default_factory=dict)
    """Each modelled coin's mark at-or-before ``at_ts`` (t0 anchor for the
    re-mark delta)."""
    collateral_value_ratios: dict[str, Decimal] = field(default_factory=dict)
    """Optional seed-locked collateral value ratios. NOT applied to
    ``total_equity`` in 0065 — stored for a future margin-balance follow-up."""
    collateral_excluded_coins: list[str] = field(default_factory=list)
    """Configured coin with no wallet row (or zero balance) at seed time."""
    collateral_missing_mark_coins: list[str] = field(default_factory=list)
    """Configured coin with a balance row but no resolvable seed mark — dropped
    from ``coin_balances`` / ``seed_marks`` / ``collateral_value_ratios``."""
    collateral_switch_off_coins: list[str] = field(default_factory=list)
    """Coin modelled for ``totalEquity`` but ``collateralSwitch`` /
    ``marginCollateral`` was false/missing (metadata / WARN only)."""


@dataclass(frozen=True)
class ActiveOrderSeed:
    """Pre-existing exchange order to inject into ``BacktestOrderManager``.

    ``client_id = order_link_id or order_id`` — the fallback covers
    pre-cross-cutting-#1 orders that were placed without an
    ``orderLinkId``. ``direction`` is derived from ``(side, reduce_only)``
    per Bybit hedge-mode rules; see module docstring.
    """

    client_id: str
    exchange_order_id: str
    symbol: str
    side: str
    direction: str
    price: Decimal
    remaining_qty: Decimal
    reduce_only: bool
    exchange_ts: datetime


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class SeedError(Exception):
    """Base class for all seed-time loader errors."""


class SeedSchemaError(SeedError):
    """A row required by the seed contract is missing a column added by
    the Phase 1 migration (e.g. ``Order.reduce_only IS NULL``).

    Recorder data captured before the migration ran is unsafe to seed
    from because direction would have to be guessed from ``side`` alone.
    """


class SeedConfigMismatchError(SeedError):
    """Saved grid ``grid_step`` / ``grid_count`` differ from the replay
    config. Reserved for future use — currently the grid loader treats a
    mismatch as a tolerant ``None`` fallback to match live.
    """


class SeedDataQualityError(SeedError):
    """Seed input is incomplete in a way that makes the replay unsafe.

    Two raise sites today:

    * Loader-local: exactly one position side is present for a run when
      both are required by the recorder's initial-REST-snapshot contract.
      The recorder writes BOTH sides (``Buy`` and ``Sell``, including
      zero-size rows) on private-stream connect; missing one side means
      the run is corrupt and seeding from it is unsafe.
    * Engine-level (0054): ``seed.enabled=True`` but no grid state could
      be loaded — both ``load_grid_state_from_snapshots`` (DB) and
      ``load_grid_state`` (file) returned ``None``. The engine raises
      this from ``_load_seed`` rather than silently falling back to a
      fresh blank-build grid (which masked the 0052 bug for 3 days).
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Bybit hedge-mode direction lookup, keyed by (side, reduce_only).
# A Buy that opens a position is long; a Buy with reduceOnly closes a
# short, so the order belongs to the short direction. Symmetrically for
# Sell. Documented in the module docstring.
_DIRECTION_BY_SIDE_REDUCE: dict[tuple[str, bool], str] = {
    ("Buy", False): "long",
    ("Sell", False): "short",
    ("Buy", True): "short",
    ("Sell", True): "long",
}


def _zero_position_seed(direction: str) -> PositionStateSeed:
    """Construct an empty seed for a direction with no recorded activity."""
    return PositionStateSeed(
        direction=direction,
        size=Decimal("0"),
        entry_price=Decimal("0"),
        liquidation_price=Decimal("0"),
    )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_grid_state(
    state_store: GridStateStore,
    strat_id: str,
    expected_step: float,
    expected_count: int,
) -> Optional[GridStateSeed]:
    """Load saved grid state for a strategy.

    Returns ``None`` (with an INFO log) on any of:

    * No saved entry for ``strat_id``.
    * Legacy anchor-only format (``GridStateStore.load`` already returns
      ``None`` and emits its own INFO log; we just propagate).
    * ``grid_step`` / ``grid_count`` differ from the replay config.

    ``None`` is the contract for the caller to handle. Under
    ``seed.enabled=True`` the replay engine raises
    :class:`SeedDataQualityError` from ``_load_seed`` once both the DB
    snapshot and this file loader return ``None`` (0054). Under
    ``seed.enabled=False`` replay may still fresh-build. JSON / IO
    errors from the store itself propagate.

    Args:
        state_store: Read-only handle to the grid-state JSON file.
        strat_id: Strategy identifier used as the key in the store.
        expected_step: Replay config ``grid_step`` for fingerprint check.
        expected_count: Replay config ``grid_count`` for fingerprint check.

    Returns:
        :class:`GridStateSeed` on a clean match, else ``None``.
    """
    entry = state_store.load(strat_id)
    if entry is None:
        # GridStateStore.load already logs INFO for the legacy-format
        # path; the no-entry path is silent in the store, so log here.
        logger.info(
            "%s: no saved grid state, will build fresh grid", strat_id,
        )
        return None

    saved_step = entry.get("grid_step")
    saved_count = entry.get("grid_count")
    if saved_step != expected_step or saved_count != expected_count:
        logger.info(
            "%s: saved grid (step=%s, count=%s) differs from replay config "
            "(step=%s, count=%s); building fresh grid",
            strat_id, saved_step, saved_count, expected_step, expected_count,
        )
        return None

    return GridStateSeed(
        strat_id=strat_id,
        grid=entry["grid"],
        grid_step=saved_step,
        grid_count=saved_count,
    )


def load_grid_state_from_snapshots(
    db_session: Session,
    account_id: str,
    strat_id: str,
    symbol: str,
    at_ts: datetime,
    expected_step: float,
    expected_count: int,
) -> Optional[GridStateSeed]:
    """Load grid state from the ``grid_state_snapshots`` DB table (0047).

    Pulls the latest snapshot at ``exchange_ts <= at_ts`` for
    ``(account_id, strat_id, symbol)``. The lookup is **cross-run** by
    design: gridbot's ``GridStateWriter`` runs under gridbot's own live
    ``run_id`` (``run_type='live'``), while the recorder — whose
    ``run_id`` reaches replay via ``seed.at_ts`` — uses an independent
    ``run_id`` (``run_type='recording'``). Scoping by recorder's
    ``run_id`` matched zero rows in production (0052).

    **Run-active guard (feature 0062).** ``get_at_or_before`` additionally
    requires the writer run was **active at ``at_ts``** — not merely
    ``exchange_ts <= at_ts``: it joins ``runs`` and demands
    ``start_ts <= at_ts``, ``end_ts`` NULL or ``>= at_ts``, and
    ``run_type`` in ``('live', 'shadow')``. This keeps the recorder-vs-live
    ``run_id`` split above (the recording run is excluded by ``run_type``)
    and, crucially, stops a completed gridbot run's last snapshot from
    seeding replay after a graceful restart, before the new run's bootstrap
    write.

    The ``symbol`` predicate prevents cross-symbol bleed-through for
    accounts whose ``strat_id`` was retained across a symbol rename
    (e.g. ``strat_id='ltcusdt_test'`` was preserved to avoid orphaning
    live grid state — the loader must not rely on ``strat_id`` alone as
    a symbol proxy).

    Returns ``None`` on no-row-found or on step/count mismatch. The
    engine tries the file path next when ``seed.grid_state_path`` is
    set, then applies the ``seed.enabled`` policy: under ``True`` it
    raises :class:`SeedDataQualityError`; under ``False`` it may still
    fresh-build (see ``engine._load_seed``).

    ``grid_step`` comparison uses ``Decimal(str(...))`` normalisation so a
    binary-imprecise float (e.g. ``0.1`` literal vs ``Decimal('0.10000000')``
    from ``Numeric(20, 8)``) doesn't false-reject.

    Pre-0047 recorder DBs do not have the ``grid_state_snapshots`` table.
    The project provisions schema via ``Base.metadata.create_all`` rather
    than Alembic, so an old DB literally does not contain this table.
    Return ``None`` (with INFO) when the table is missing so the engine's
    file fallback at ``engine.py:_load_seed`` runs.
    """
    # Use ``session.connection()`` (NOT ``get_bind()``) so the inspector
    # shares the session's open transaction. ``inspect(engine).has_table``
    # would acquire a fresh connection from the pool — on SQLite
    # ``:memory:`` + StaticPool this issues a ROLLBACK that wipes the
    # session's uncommitted writes.
    if not sa_inspect(db_session.connection()).has_table(
        GridStateSnapshot.__tablename__,
    ):
        logger.info(
            "%s: grid_state_snapshots table not present (pre-0047 DB); "
            "falling back to file path / fresh build",
            strat_id,
        )
        return None
    row = GridStateSnapshotRepository(db_session).get_at_or_before(
        account_id, strat_id, symbol, at_ts,
    )
    if row is None:
        logger.info(
            "%s: no grid snapshot at-or-before %s", strat_id, at_ts,
        )
        return None
    if int(row.grid_count) != int(expected_count):
        logger.info(
            "%s: saved grid_count=%d differs from replay config %d; falling back",
            strat_id, row.grid_count, expected_count,
        )
        return None
    if Decimal(str(row.grid_step)) != Decimal(str(expected_step)):
        logger.info(
            "%s: saved grid_step=%s differs from replay config %s; falling back",
            strat_id, row.grid_step, expected_step,
        )
        return None
    logger.info(
        "%s: grid snapshot loaded from run_id=%s exchange_ts=%s",
        strat_id, row.run_id, row.exchange_ts,
    )
    return GridStateSeed(
        strat_id=strat_id,
        grid=row.grid_json,
        grid_step=float(row.grid_step),
        grid_count=int(row.grid_count),
    )


def load_position_snapshots(
    db_session: Session,
    run_id: str,
    account_id: str,
    symbol: str,
    at_ts: datetime,
) -> tuple[PositionStateSeed, PositionStateSeed]:
    """Load the (long, short) position seed pair for a recorder run.

    The recorder's initial REST snapshot writes BOTH sides on
    private-stream connect (including zero-size rows for the absent
    side). The loader leans on that invariant:

    * Both sides present  → seed both from the snapshot rows.
    * Both sides absent   → ``(zero, zero)``. Phase 4 pre-check rejects
      this case before replay starts when the snapshot has not yet
      landed, so reaching it here means the run legitimately had no
      activity AND no initial snapshot — the (zero, zero) is harmless.
    * Exactly one side    → :class:`SeedDataQualityError`. The recorder
      run is corrupt; seeding would assume the wrong default for the
      missing side.

    Snapshot ``liq_price`` may be ``NULL``; we coerce to ``Decimal('0')``.

    Args:
        db_session: Read-only DB session.
        run_id: Recorder run identifier.
        account_id: Account ID.
        symbol: Trading symbol (e.g. ``'BTCUSDT'``).
        at_ts: Inclusive upper bound on ``exchange_ts``.

    Returns:
        Tuple ``(long_seed, short_seed)``.

    Raises:
        SeedDataQualityError: Exactly one side has no rows for this run.
    """
    _probe_position_schema(db_session)
    repo = PositionSnapshotRepository(db_session)
    buy_snap = repo.get_latest_before(run_id, account_id, symbol, "Buy", at_ts)
    sell_snap = repo.get_latest_before(run_id, account_id, symbol, "Sell", at_ts)

    if buy_snap is None and sell_snap is None:
        return _zero_position_seed("long"), _zero_position_seed("short")

    if buy_snap is None or sell_snap is None:
        present = "Buy" if buy_snap is not None else "Sell"
        missing = "Sell" if buy_snap is not None else "Buy"
        raise SeedDataQualityError(
            f"Position snapshot for run_id={run_id}, account_id={account_id}, "
            f"symbol={symbol} has only side={present} (missing {missing}); "
            "recorder's initial REST snapshot must write both sides — run is corrupt"
        )

    long_seed = PositionStateSeed(
        direction="long",
        size=buy_snap.size,
        entry_price=buy_snap.entry_price,
        liquidation_price=buy_snap.liq_price if buy_snap.liq_price is not None else Decimal("0"),
        cum_realised_pnl=(
            buy_snap.cum_realised_pnl if buy_snap.cum_realised_pnl is not None else Decimal("0")
        ),
        cur_realised_pnl=(
            buy_snap.cur_realised_pnl if buy_snap.cur_realised_pnl is not None else Decimal("0")
        ),
    )
    short_seed = PositionStateSeed(
        direction="short",
        size=sell_snap.size,
        entry_price=sell_snap.entry_price,
        liquidation_price=sell_snap.liq_price if sell_snap.liq_price is not None else Decimal("0"),
        cum_realised_pnl=(
            sell_snap.cum_realised_pnl if sell_snap.cum_realised_pnl is not None else Decimal("0")
        ),
        cur_realised_pnl=(
            sell_snap.cur_realised_pnl if sell_snap.cur_realised_pnl is not None else Decimal("0")
        ),
    )
    return long_seed, short_seed


def load_wallet_snapshot(
    db_session: Session,
    run_id: str,
    account_id: str,
    at_ts: datetime,
    coin: str = "USDT",
) -> Optional[Decimal]:
    """Load the latest wallet balance for a run/account/coin.

    Returns ``None`` when no snapshot exists for this combination — the
    caller (``ReplayEngine``) decides whether to fall back to
    ``config.initial_balance``. There is no global staleness threshold:
    Bybit private streams are event-driven and a quiet wallet may sit
    unchanged for arbitrary periods.

    Args:
        db_session: Read-only DB session.
        run_id: Recorder run identifier.
        account_id: Account ID.
        at_ts: Inclusive upper bound on ``exchange_ts``.
        coin: Coin symbol; defaults to ``'USDT'``.

    Returns:
        Latest ``wallet_balance`` as a :class:`Decimal`, or ``None``.
    """
    repo = WalletSnapshotRepository(db_session)
    snap = repo.get_latest_before(run_id, account_id, coin, at_ts)
    if snap is None:
        return None
    return snap.wallet_balance


def load_wallet_seed_full(
    db_session: Session,
    run_id: str,
    account_id: str,
    at_ts: datetime,
    coin: str = "USDT",
) -> Optional[WalletSeed]:
    """Load the latest 0042 wallet seed for a run/account/coin.

    Returns ``None`` when:

    - No snapshot exists, OR
    - ``total_available_balance IS NULL`` (legacy pre-0042 row), OR
    - ``total_equity IS NULL`` while ``total_available_balance`` is populated, OR
    - ``total_equity <= 0`` or ``total_available_balance <= 0`` (defensive).

    The non-positive guards catch two distinct failure modes that would
    otherwise silently corrupt either the 0043 pair-liq formula
    (``total_equity``) or the 0042 executor margin gating / qty calculator /
    risk multiplier inputs (``total_available_balance``):

    1. WS-writer fallback. ``wallet_writer._messages_to_models`` uses
       ``decimal_or_zero(...)`` which maps both ``None`` and ``""`` to
       ``Decimal(0)``. If a future Bybit payload shape drops one of the
       account-level keys entirely, the DB row stores ``0`` rather than
       ``NULL`` and the ``is None`` guard above misses it.
    2. Genuinely zero baselines. A fully-margined or empty account has no
       meaningful replay interpretation — simulation cannot open new
       orders with zero available margin.

    Both produce the same outcome: refuse and let replay fall back to
    ``config.initial_balance``.
    """
    repo = WalletSnapshotRepository(db_session)
    snap = repo.get_latest_before(run_id, account_id, coin, at_ts)
    if snap is None or snap.total_available_balance is None:
        return None
    if snap.total_equity is None:
        logger.warning(
            "Wallet snapshot for run_id=%s account_id=%s coin=%s has "
            "total_available_balance but NULL total_equity; refusing to seed "
            "with zero equity baseline. Re-record after the 0042 migration "
            "so writes populate both columns.",
            run_id, account_id, coin,
        )
        return None
    if snap.total_equity <= 0:
        logger.warning(
            "Wallet snapshot for run_id=%s account_id=%s coin=%s has "
            "total_equity=%s (<= 0); refusing to seed. Possible causes: "
            "Bybit payload missing the totalEquity key (writer stored 0 via "
            "decimal_or_zero), or the account is empty. Replay will fall "
            "back to config.initial_balance.",
            run_id, account_id, coin, snap.total_equity,
        )
        return None
    if snap.total_available_balance <= 0:
        logger.warning(
            "Wallet snapshot for run_id=%s account_id=%s coin=%s has "
            "total_available_balance=%s (<= 0); refusing to seed. Possible "
            "causes: Bybit payload missing the totalAvailableBalance key "
            "(writer stored 0 via decimal_or_zero), or the account is "
            "fully margined. Replay will fall back to config.initial_balance.",
            run_id, account_id, coin, snap.total_available_balance,
        )
        return None

    return WalletSeed(
        coin_balance=snap.wallet_balance,
        total_available_balance=snap.total_available_balance,
        total_equity=snap.total_equity,
        total_margin_balance=(
            snap.total_margin_balance
            if snap.total_margin_balance is not None
            else Decimal("0")
        ),
        account_im_rate=(
            snap.account_im_rate if snap.account_im_rate is not None else Decimal("0")
        ),
        account_mm_rate=(
            snap.account_mm_rate if snap.account_mm_rate is not None else Decimal("0")
        ),
    )


def load_collateral_seed(
    db_session: Session,
    run_id: str,
    account_id: str,
    at_ts: datetime,
    usdt_coin: str,
    collateral_coins: list[str],
    collateral_symbol_for: dict[str, str],
    configured_value_ratios: dict[str, Decimal],
    wallet_max_staleness: timedelta,
) -> tuple[
    dict[str, Decimal],  # coin_balances
    dict[str, Decimal],  # seed_marks
    dict[str, Decimal],  # collateral_value_ratios
    list[str],           # excluded_coins
    list[str],           # missing_mark_coins
    list[str],           # switch_off_coins
]:
    """Resolve per-coin balances + seed marks for non-USDT collateral re-marking.

    Feature 0065. Bybit account ``totalEquity`` is the USD sum of per-asset
    equity regardless of whether the asset is enabled as margin collateral, so
    inclusion is gated purely on the operator list ``collateral_coins`` plus a
    wallet row with ``wallet_balance > 0`` at-or-before ``at_ts`` — NOT on the
    ``collateralSwitch`` / ``marginCollateral`` booleans (those are recorded as
    metadata only).

    Seed mark resolution per coin (valuation basis):

    * **Preferred** ``usdValue / wallet_balance`` — only when the coin's wallet
      row is FRESH vs ``at_ts`` (``_strip_tz(at_ts) - _strip_tz(row.exchange_ts)
      <= wallet_max_staleness``) and ``usdValue`` is present. A quiet collateral
      coin's per-coin row can be much older than the account-level USDT row that
      ``initial_equity`` comes from; using its stale ``usdValue`` would inject a
      false jump on the first ticker update.
    * **Fallback** ``TickerSnapshotRepository.get_mark_at_or_before(symbol,
      at_ts)`` — used when the row is stale or has no ``usdValue``.

    A coin with a balance row but no usable seed mark is dropped from all three
    model dicts and reported in ``missing_mark_coins`` (live ``totalEquity``
    still includes it; the #3a gap surfaces it).

    Returns:
        ``(coin_balances, seed_marks, collateral_value_ratios, excluded_coins,
        missing_mark_coins, switch_off_coins)``. The first three dicts contain
        ONLY fully-modelled coins (every ``coin_balances`` key has a matching
        ``seed_marks`` key).
    """
    coin_balances: dict[str, Decimal] = {}
    seed_marks: dict[str, Decimal] = {}
    value_ratios: dict[str, Decimal] = {}
    excluded_coins: list[str] = []
    missing_mark_coins: list[str] = []
    switch_off_coins: list[str] = []

    if not collateral_coins:
        return (
            coin_balances, seed_marks, value_ratios,
            excluded_coins, missing_mark_coins, switch_off_coins,
        )

    wallet_repo = WalletSnapshotRepository(db_session)
    ticker_repo = TickerSnapshotRepository(db_session)

    rows = wallet_repo.get_all_coins_latest_before(run_id, account_id, at_ts)
    # Dedup by coin (last wins) — the repo may return ties on exchange_ts.
    rows_by_coin: dict[str, WalletSnapshot] = {}
    for row in rows:
        rows_by_coin[row.coin] = row

    at_ts_naive = _strip_tz(at_ts)

    for coin in collateral_coins:
        if coin == usdt_coin:
            # The USDT/base coin is seeded via initial_equity, not re-marked.
            continue

        row = rows_by_coin.get(coin)
        # Inclusion rule is wallet_balance > 0 — exclude missing, None, zero,
        # AND negative balances (a borrowed/short asset is not spot collateral
        # and would invert the drift contribution if re-marked).
        if row is None or row.wallet_balance is None or row.wallet_balance <= 0:
            excluded_coins.append(coin)
            logger.warning(
                "collateral coin %s has no wallet row (or non-positive balance) "
                "at at_ts=%s; excluding from totalEquity re-mark (cannot model).",
                coin, at_ts,
            )
            continue

        balance = row.wallet_balance
        symbol = collateral_symbol_for.get(coin, f"{coin}USDT")
        raw = row.raw_json or {}
        row_age = at_ts_naive - _strip_tz(row.exchange_ts)
        usd_value_raw = raw.get("usdValue")

        # Resolve seed mark: fresh usdValue/balance, else carry-forward ticker.
        # ``balance`` is already guaranteed non-zero by the exclusion gate
        # above, so only a malformed usdValue string can fail here.
        seed_mark: Optional[Decimal] = None
        if usd_value_raw not in (None, "") and row_age <= wallet_max_staleness:
            try:
                seed_mark = Decimal(str(usd_value_raw)) / balance
            except InvalidOperation:
                logger.warning(
                    "malformed usdValue=%r in %s wallet row; falling back to "
                    "ticker mark for seed.",
                    usd_value_raw, coin,
                )
                seed_mark = None
        if seed_mark is None:
            if usd_value_raw not in (None, "") and row_age > wallet_max_staleness:
                logger.warning(
                    "stale wallet row for %s seed mark (age %s > %s); using "
                    "ticker mark at at_ts instead.",
                    coin, row_age, wallet_max_staleness,
                )
            seed_mark = ticker_repo.get_mark_at_or_before(symbol, at_ts)

        if seed_mark is None:
            missing_mark_coins.append(coin)
            logger.warning(
                "collateral coin %s has a balance row but no usable seed mark "
                "(no fresh usdValue and no %s ticker at-or-before at_ts=%s); "
                "dropping from the modelled re-mark set.",
                coin, symbol, at_ts,
            )
            continue

        # Commit the fully-modelled coin.
        seed_marks[coin] = seed_mark
        coin_balances[coin] = balance
        if coin in configured_value_ratios:
            value_ratios[coin] = configured_value_ratios[coin]

        # Boolean metadata only — never gates inclusion (Bybit totalEquity
        # ignores the collateral switch). WARN when off/missing.
        col_switch = raw.get("collateralSwitch")
        margin_col = raw.get("marginCollateral")
        if not col_switch or not margin_col:
            switch_off_coins.append(coin)
            logger.warning(
                "collateral coin %s has collateralSwitch=%s marginCollateral=%s "
                "(off/missing); still re-marked for totalEquity (metadata only).",
                coin, col_switch, margin_col,
            )

    return (
        coin_balances, seed_marks, value_ratios,
        excluded_coins, missing_mark_coins, switch_off_coins,
    )


def load_active_orders(
    db_session: Session,
    run_id: str,
    account_id: str,
    symbol: str,
    at_ts: datetime,
) -> list[ActiveOrderSeed]:
    """Load the set of open orders that existed live at ``at_ts``.

    Returns ``[]`` for a clean account with no live orders — that is a
    valid happy path (an empty grid that has not placed yet). Each
    returned seed has ``client_id = order_link_id or order_id`` so the
    comparator can match across the ``orderLinkId``-introduction window.

    ``direction`` is derived from ``(side, reduce_only)`` per Bybit
    hedge-mode rules (see module docstring).

    Args:
        db_session: Read-only DB session.
        run_id: Recorder run identifier.
        account_id: Account ID.
        symbol: Trading symbol.
        at_ts: Inclusive upper bound on ``exchange_ts``.

    Returns:
        List of :class:`ActiveOrderSeed`, ordered as the repository
        returned them (insertion order; the simulator does not depend
        on a particular ordering).

    Raises:
        SeedSchemaError: A returned row has ``reduce_only IS NULL``,
            indicating it predates the Phase 1 schema migration.
    """
    repo = OrderRepository(db_session)
    rows = repo.get_active_at(run_id, account_id, symbol, at_ts)
    seeds: list[ActiveOrderSeed] = []
    for row in rows:
        if row.reduce_only is None:
            raise SeedSchemaError(
                f"Order order_id={row.order_id} (run_id={run_id}, account_id="
                f"{account_id}, symbol={symbol}) has reduce_only=NULL; "
                "row predates the Phase 1 migration and cannot be safely seeded"
            )
        direction = _DIRECTION_BY_SIDE_REDUCE.get((row.side, bool(row.reduce_only)))
        if direction is None:
            # Defensive: side is constrained to 'Buy'/'Sell' at the schema
            # level, so the lookup should always succeed. If a future writer
            # introduces a new side value, fail loudly rather than guess.
            raise SeedSchemaError(
                f"Order order_id={row.order_id} has unexpected side={row.side!r}; "
                "expected 'Buy' or 'Sell'"
            )
        # Strip the post-hotfix `-{millis}` suffix from order_link_id so the
        # seed key matches replay's deterministic client_order_id prefix.
        # See gridcore.intents.extract_client_order_prefix for rationale.
        client_id = extract_client_order_prefix(row.order_link_id) or row.order_id
        seeds.append(
            ActiveOrderSeed(
                client_id=client_id,
                exchange_order_id=row.order_id,
                symbol=row.symbol,
                side=row.side,
                direction=direction,
                price=row.price,
                remaining_qty=row.leaves_qty,
                reduce_only=bool(row.reduce_only),
                exchange_ts=row.exchange_ts,
            )
        )
    return seeds


__all__ = [
    "GridStateSeed",
    "PositionStateSeed",
    "WalletSeed",
    "ActiveOrderSeed",
    "SeedError",
    "SeedSchemaError",
    "SeedConfigMismatchError",
    "SeedDataQualityError",
    "load_grid_state",
    "load_grid_state_from_snapshots",
    "load_position_snapshots",
    "load_wallet_seed_full",
    "load_wallet_snapshot",
    "load_active_orders",
]
