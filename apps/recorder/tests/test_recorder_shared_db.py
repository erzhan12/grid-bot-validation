"""Integration tests for Recorder ↔ Gridbot shared-DB identity (Phase 5b).

Asserts that the recorder's `_seed_db_records` verify-only branch:
- does NOT mutate gridbot-written User/BybitAccount/Strategy rows,
- does NOT violate the `(account_id, symbol)` Strategy unique constraint,
- successfully inserts a Run row whose FKs resolve to gridbot's parents,
- raises `RuntimeError` wrapping `SharedDbParentError` when any parent
  row is missing or has incompatible metadata.
"""

from __future__ import annotations

import asyncio

import pytest

from grid_db import BybitAccount, Run, Strategy, User
from grid_db.identity import account_id_for, strategy_id_for, user_id_for
from recorder.config import AccountConfig, RecorderConfig
from recorder.recorder import Recorder
from recorder.shared_db_parents import SharedDbParentError


# --- Helpers --------------------------------------------------------------


def _config(name: str = "mainnet_live", strat_id: str = "ltcusdt_test",
            symbols: list[str] | None = None, testnet: bool = False) -> RecorderConfig:
    return RecorderConfig(
        symbols=symbols if symbols is not None else ["LTCUSDT"],
        capture_public_trades=False,
        database_url="sqlite:///:memory:",
        testnet=testnet,
        batch_size=10,
        flush_interval=1.0,
        health_log_interval=60.0,
        account=AccountConfig(
            name=name,
            strat_id=strat_id,
            api_key="test_key",
            api_secret="test_secret",
        ),
    )


def _seed_gridbot_parents(
    db,
    *,
    name: str = "mainnet_live",
    strat_id: str = "ltcusdt_test",
    environment: str = "mainnet",
    symbol: str = "LTCUSDT",
    strategy_type: str = "GridStrategy",
    config_json: dict | None = None,
) -> None:
    with db.get_session() as session:
        session.add(User(user_id=user_id_for(name), username=name))
        session.add(
            BybitAccount(
                account_id=account_id_for(name),
                user_id=user_id_for(name),
                account_name=name,
                environment=environment,
            )
        )
        session.add(
            Strategy(
                strategy_id=strategy_id_for(strat_id),
                account_id=account_id_for(name),
                strategy_type=strategy_type,
                symbol=symbol,
                config_json=config_json or {"grid_levels": 20, "grid_step": 0.3},
            )
        )


# --- Positive case --------------------------------------------------------


async def test_seed_does_not_mutate_gridbot_parents(db):
    _seed_gridbot_parents(db)

    recorder = Recorder(config=_config(), db=db)
    await asyncio.to_thread(recorder._seed_db_records)

    with db.get_session() as session:
        user = session.get(User, user_id_for("mainnet_live"))
        assert user is not None
        assert user.username == "mainnet_live"

        account = session.get(BybitAccount, account_id_for("mainnet_live"))
        assert account is not None
        assert account.account_name == "mainnet_live"
        assert account.environment == "mainnet"

        strategy = session.get(Strategy, strategy_id_for("ltcusdt_test"))
        assert strategy is not None
        assert strategy.strategy_type == "GridStrategy"
        assert strategy.config_json == {"grid_levels": 20, "grid_step": 0.3}

        runs = session.query(Run).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.user_id == user_id_for("mainnet_live")
        assert run.account_id == account_id_for("mainnet_live")
        assert run.strategy_id == strategy_id_for("ltcusdt_test")
        assert run.run_type == "recording"

    assert str(recorder._account_id) == account_id_for("mainnet_live")
    assert str(recorder._user_id) == user_id_for("mainnet_live")
    assert str(recorder._strategy_id) == strategy_id_for("ltcusdt_test")


# --- Negative cases -------------------------------------------------------


async def test_seed_fails_when_user_missing(db):
    # Empty DB — no parent rows at all.
    recorder = Recorder(config=_config(), db=db)
    with pytest.raises(RuntimeError) as exc:
        await asyncio.to_thread(recorder._seed_db_records)
    cause = exc.value.__cause__
    assert isinstance(cause, SharedDbParentError)
    assert "User" in str(cause)


async def test_seed_fails_when_bybit_account_missing(db):
    name = "mainnet_live"
    with db.get_session() as session:
        session.add(User(user_id=user_id_for(name), username=name))

    recorder = Recorder(config=_config(name=name), db=db)
    with pytest.raises(RuntimeError) as exc:
        await asyncio.to_thread(recorder._seed_db_records)
    cause = exc.value.__cause__
    assert isinstance(cause, SharedDbParentError)
    assert "BybitAccount" in str(cause)


async def test_seed_fails_when_strategy_missing(db):
    name = "mainnet_live"
    with db.get_session() as session:
        session.add(User(user_id=user_id_for(name), username=name))
        session.add(
            BybitAccount(
                account_id=account_id_for(name),
                user_id=user_id_for(name),
                account_name=name,
                environment="mainnet",
            )
        )

    recorder = Recorder(config=_config(name=name), db=db)
    with pytest.raises(RuntimeError) as exc:
        await asyncio.to_thread(recorder._seed_db_records)
    cause = exc.value.__cause__
    assert isinstance(cause, SharedDbParentError)
    assert "Strategy" in str(cause)


async def test_seed_fails_on_environment_mismatch(db):
    _seed_gridbot_parents(db, environment="mainnet")
    # Recorder config says testnet but BybitAccount has environment='mainnet'.
    recorder = Recorder(config=_config(testnet=True), db=db)
    with pytest.raises(RuntimeError) as exc:
        await asyncio.to_thread(recorder._seed_db_records)
    cause = exc.value.__cause__
    assert isinstance(cause, SharedDbParentError)
    assert "environment" in str(cause)


async def test_seed_fails_on_account_user_id_mismatch(db):
    # Seed a BybitAccount whose user_id points at DIFFERENT_NAME, not the
    # name the recorder is configured with. Must also seed BOTH Users so
    # the BybitAccount FK insert succeeds during setup.
    name = "test_account"
    other = "DIFFERENT_NAME"
    with db.get_session() as session:
        session.add(User(user_id=user_id_for(name), username=name))
        session.add(User(user_id=user_id_for(other), username=other))
        session.add(
            BybitAccount(
                account_id=account_id_for(name),
                user_id=user_id_for(other),
                account_name=name,
                environment="testnet",
            )
        )
        session.add(
            Strategy(
                strategy_id=strategy_id_for("test_strat"),
                account_id=account_id_for(name),
                strategy_type="GridStrategy",
                symbol="LTCUSDT",
                config_json={},
            )
        )

    recorder = Recorder(
        config=_config(name=name, strat_id="test_strat", testnet=True),
        db=db,
    )
    with pytest.raises(RuntimeError) as exc:
        await asyncio.to_thread(recorder._seed_db_records)
    cause = exc.value.__cause__
    assert isinstance(cause, SharedDbParentError)
    assert "user_id" in str(cause)


async def test_seed_fails_on_strategy_account_id_mismatch(db):
    name = "test_account"
    other = "OTHER_ACCOUNT"
    # Seed OTHER_ACCOUNT's full chain first (so its BybitAccount FK works).
    with db.get_session() as session:
        session.add(User(user_id=user_id_for(other), username=other))
        session.add(
            BybitAccount(
                account_id=account_id_for(other),
                user_id=user_id_for(other),
                account_name=other,
                environment="testnet",
            )
        )
        # Now seed test_account's User + BybitAccount.
        session.add(User(user_id=user_id_for(name), username=name))
        session.add(
            BybitAccount(
                account_id=account_id_for(name),
                user_id=user_id_for(name),
                account_name=name,
                environment="testnet",
            )
        )
        # Strategy owned by OTHER_ACCOUNT but keyed on test_strat's uuid5.
        session.add(
            Strategy(
                strategy_id=strategy_id_for("test_strat"),
                account_id=account_id_for(other),
                strategy_type="GridStrategy",
                symbol="LTCUSDT",
                config_json={},
            )
        )

    recorder = Recorder(
        config=_config(name=name, strat_id="test_strat", testnet=True),
        db=db,
    )
    with pytest.raises(RuntimeError) as exc:
        await asyncio.to_thread(recorder._seed_db_records)
    cause = exc.value.__cause__
    assert isinstance(cause, SharedDbParentError)
    assert "account_id" in str(cause)


async def test_seed_fails_on_strategy_type_mismatch(db):
    _seed_gridbot_parents(db, strategy_type="recorder")
    recorder = Recorder(config=_config(), db=db)
    with pytest.raises(RuntimeError) as exc:
        await asyncio.to_thread(recorder._seed_db_records)
    cause = exc.value.__cause__
    assert isinstance(cause, SharedDbParentError)
    assert "strategy_type" in str(cause)


async def test_seed_fails_on_symbol_mismatch(db):
    _seed_gridbot_parents(db, symbol="ETHUSDT")
    # Recorder primary symbol is BTCUSDT; gridbot Strategy row is for ETHUSDT.
    recorder = Recorder(config=_config(symbols=["BTCUSDT"]), db=db)
    with pytest.raises(RuntimeError) as exc:
        await asyncio.to_thread(recorder._seed_db_records)
    cause = exc.value.__cause__
    assert isinstance(cause, SharedDbParentError)
    assert "symbol" in str(cause).lower()
