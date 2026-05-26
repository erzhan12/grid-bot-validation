"""Verify gridbot-owned parent rows before recorder Run insert.

Shared-DB mode (Phase 4 of feature 0029): the recorder consumes the
`User`, `BybitAccount`, and `Strategy` rows that gridbot creates. The
recorder must not mutate those rows (PK-driven `session.merge` would
silently overwrite gridbot metadata). Instead, verify they exist with
gridbot-compatible metadata and raise on mismatch so the operator sees a
clear error at prepare or recorder startup — not after the recorder has
written hours of data under a divergent identity.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from grid_db import BybitAccount, Strategy, User


class SharedDbParentError(RuntimeError):
    """Raised when shared-DB parent rows are missing or have incompatible metadata."""


def verify_shared_db_parents(
    session: Session,
    *,
    user_id: str,
    account_id: str,
    strategy_id: str,
    account_name: str,
    strat_id: str,
    primary_symbol: str,
    recorder_testnet: bool,
) -> None:
    """3 existence + 5 metadata checks. All failures raise SharedDbParentError.

    Args:
        session: open SQLAlchemy session
        user_id / account_id / strategy_id: str UUIDs (uuid5 of account/strat names)
        account_name / strat_id: used only in error messages
        primary_symbol: recorder's first configured symbol; must equal
            ``strategy_row.symbol`` to catch recorder/gridbot symbol drift
        recorder_testnet: recorder's testnet flag; must match
            ``bybit_account.environment`` ("testnet"/"mainnet")
    """
    expected_environment = "testnet" if recorder_testnet else "mainnet"

    user_row = session.get(User, user_id)
    if user_row is None:
        raise SharedDbParentError(
            f"Shared-DB mode requires gridbot User row for {account_name!r}; not found"
        )

    account_row = session.get(BybitAccount, account_id)
    if account_row is None:
        raise SharedDbParentError(
            f"Shared-DB mode requires gridbot BybitAccount row for {account_name!r}; "
            f"not found"
        )
    if account_row.user_id != user_id:
        raise SharedDbParentError(
            f"BybitAccount.user_id mismatch for {account_name!r}: "
            f"expected {user_id!r}, found {account_row.user_id!r}"
        )
    if account_row.environment != expected_environment:
        raise SharedDbParentError(
            f"BybitAccount.environment mismatch for {account_name!r}: "
            f"recorder testnet={recorder_testnet} expects "
            f"{expected_environment!r}, found {account_row.environment!r}"
        )

    strategy_row = session.get(Strategy, strategy_id)
    if strategy_row is None:
        raise SharedDbParentError(
            f"Shared-DB mode requires gridbot Strategy row for strat_id "
            f"{strat_id!r} (account {account_name!r}); not found"
        )
    if strategy_row.account_id != account_id:
        raise SharedDbParentError(
            f"Strategy.account_id mismatch for strat_id {strat_id!r}: "
            f"expected {account_id!r}, found {strategy_row.account_id!r}"
        )
    if strategy_row.strategy_type != "GridStrategy":
        raise SharedDbParentError(
            f"Strategy.strategy_type mismatch for strat_id {strat_id!r}: "
            f"expected 'GridStrategy', found {strategy_row.strategy_type!r}"
        )
    if strategy_row.symbol != primary_symbol:
        raise SharedDbParentError(
            f"Strategy.symbol mismatch for strat_id {strat_id!r}: "
            f"recorder primary symbol {primary_symbol!r}, gridbot Strategy "
            f"row symbol {strategy_row.symbol!r}"
        )
