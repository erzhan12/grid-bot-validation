"""Tests for `recorder.prepare_session` (Phase 4c)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from grid_db import (
    BybitAccount,
    DatabaseFactory,
    DatabaseSettings,
    Run,
    Strategy,
    User,
)
from grid_db.identity import account_id_for, strategy_id_for, user_id_for

from recorder.prepare_session import _sqlite_filesystem_path, main
from recorder.shared_db_parents import (
    SharedDbParentError,
    verify_shared_db_parents,
)


# --- Helpers --------------------------------------------------------------


def _write_recorder_yaml(
    path: Path,
    *,
    db_path: Path,
    with_account: bool = True,
    name: str = "mainnet_live",
    strat_id: str = "ltcusdt_test",
    testnet: bool = False,
    symbols: list[str] | None = None,
    use_env_var: bool = False,
) -> None:
    data: dict = {
        "symbols": symbols if symbols is not None else ["LTCUSDT"],
        "database_url": (
            f"sqlite:///{db_path}" if not use_env_var else "${TEST_DB_URL}"
        ),
        "testnet": testnet,
    }
    if with_account:
        data["account"] = {
            "name": name,
            "strat_id": strat_id,
            "api_key": "test_key",
            "api_secret": "test_secret",
        }
    path.write_text(yaml.safe_dump(data))


def _write_gridbot_yaml(
    path: Path,
    *,
    db_path: Path,
    account_name: str = "mainnet_live",
    account_testnet: bool = False,
    strat_id: str = "ltcusdt_test",
    symbol: str = "LTCUSDT",
    extra_strategies: list[dict] | None = None,
) -> None:
    strategies = [
        {
            "strat_id": strat_id,
            "account": account_name,
            "symbol": symbol,
            "tick_size": "0.01",
        }
    ]
    if extra_strategies:
        strategies.extend(extra_strategies)
    data = {
        "accounts": [
            {
                "name": account_name,
                "api_key": "k",
                "api_secret": "s",
                "testnet": account_testnet,
            }
        ],
        "strategies": strategies,
        "database_url": f"sqlite:///{db_path}",
    }
    path.write_text(yaml.safe_dump(data))


def _factory(db_path: Path) -> DatabaseFactory:
    settings = DatabaseSettings(database_url=f"sqlite:///{db_path}")
    return DatabaseFactory(settings)


def _seed_full_parent_chain(
    factory: DatabaseFactory,
    *,
    name: str = "mainnet_live",
    strat_id: str = "ltcusdt_test",
    environment: str = "mainnet",
    symbol: str = "LTCUSDT",
    strategy_type: str = "GridStrategy",
) -> None:
    with factory.get_session() as session:
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
                config_json={},
            )
        )


# --- _sqlite_filesystem_path ----------------------------------------------


def test_sqlite_path_parsing_accepts_pysqlite_driver(tmp_path):
    p = tmp_path / "test.db"
    assert _sqlite_filesystem_path(f"sqlite+pysqlite:///{p}") == str(p)


def test_sqlite_path_parsing_in_memory_returns_none():
    assert _sqlite_filesystem_path("sqlite:///:memory:") is None


def test_sqlite_path_parsing_rejects_non_sqlite():
    with pytest.raises(ValueError, match="SQLite"):
        _sqlite_filesystem_path("postgresql://u:p@h/db")


# --- _wipe_recorder_data --------------------------------------------------


def test_wipe_uses_expanded_database_url(tmp_path, monkeypatch):
    db_path = tmp_path / "expanded.db"
    monkeypatch.setenv("TEST_DB_URL", f"sqlite:///{db_path}")
    factory = _factory(db_path)
    factory.create_tables()
    _seed_full_parent_chain(factory)
    # Insert a recording run so we can prove the wipe ran.
    with factory.get_session() as session:
        session.add(
            Run(
                user_id=user_id_for("mainnet_live"),
                account_id=account_id_for("mainnet_live"),
                strategy_id=strategy_id_for("ltcusdt_test"),
                run_type="recording",
                status="running",
            )
        )

    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path, use_env_var=True)
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path)

    rc = main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert rc == 0

    with factory.engine.connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM runs WHERE run_type = 'recording'")
        ).scalar_one()
    assert n == 0


def test_wipe_succeeds_on_existing_empty_schemaless_file(tmp_path):
    """Empty-but-existing SQLite file (touch'd, mounted empty, interrupted
    first run) must not crash the wipe DELETEs with `no such table: ...`."""
    db_path = tmp_path / "empty.db"
    db_path.touch()  # exists, zero bytes, no schema

    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path)
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path)

    rc = main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert rc == 0

    factory = _factory(db_path)
    with factory.get_session() as session:
        assert session.get(User, user_id_for("mainnet_live")) is not None


# --- main() with no `account:` block --------------------------------------


def test_bootstrap_skips_when_no_account_block(tmp_path):
    db_path = tmp_path / "no_account.db"
    factory = _factory(db_path)
    factory.create_tables()

    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path, with_account=False)
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path)

    rc = main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert rc == 0

    # No parent rows should have been created.
    with factory.get_session() as session:
        assert session.get(User, user_id_for("mainnet_live")) is None
        assert session.get(BybitAccount, account_id_for("mainnet_live")) is None
        assert session.get(Strategy, strategy_id_for("ltcusdt_test")) is None


# --- Bootstrap positive paths ---------------------------------------------


def test_bootstrap_creates_parents_on_clean_db(tmp_path, capsys):
    db_path = tmp_path / "clean.db"
    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path)
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path)

    rc = main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert rc == 0

    factory = _factory(db_path)
    with factory.get_session() as session:
        assert session.get(User, user_id_for("mainnet_live")) is not None
        assert session.get(BybitAccount, account_id_for("mainnet_live")) is not None
        assert session.get(Strategy, strategy_id_for("ltcusdt_test")) is not None
    out = capsys.readouterr().out
    assert "created" in out


def test_bootstrap_idempotent_when_parents_valid(tmp_path, capsys):
    db_path = tmp_path / "idempotent.db"
    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path)
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path)

    rc1 = main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert rc1 == 0
    rc2 = main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert rc2 == 0

    out = capsys.readouterr().out
    assert "already present" in out

    factory = _factory(db_path)
    with factory.engine.connect() as conn:
        n_users = conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
        n_accounts = conn.execute(text("SELECT COUNT(*) FROM bybit_accounts")).scalar_one()
        n_strats = conn.execute(text("SELECT COUNT(*) FROM strategies")).scalar_one()
    assert n_users == 1
    assert n_accounts == 1
    assert n_strats == 1


def test_bootstrap_does_not_create_runs(tmp_path):
    db_path = tmp_path / "no_runs.db"
    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path)
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path)

    rc = main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert rc == 0

    factory = _factory(db_path)
    with factory.engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM runs")).scalar_one()
    assert n == 0


# --- verify_shared_db_parents direct unit tests (stale rows) --------------


def test_verify_raises_on_stale_strategy_type(tmp_path):
    db_path = tmp_path / "stale_strat.db"
    factory = _factory(db_path)
    factory.create_tables()
    _seed_full_parent_chain(factory, strategy_type="recorder")

    with factory.get_session() as session:
        with pytest.raises(SharedDbParentError, match="strategy_type"):
            verify_shared_db_parents(
                session,
                user_id=user_id_for("mainnet_live"),
                account_id=account_id_for("mainnet_live"),
                strategy_id=strategy_id_for("ltcusdt_test"),
                account_name="mainnet_live",
                strat_id="ltcusdt_test",
                primary_symbol="LTCUSDT",
                recorder_testnet=False,
            )


def test_prepare_fails_when_existing_strategy_type_stale(tmp_path, capsys):
    db_path = tmp_path / "stale_strat_main.db"
    factory = _factory(db_path)
    factory.create_tables()
    _seed_full_parent_chain(factory, strategy_type="recorder")

    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path)
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path)

    rc = main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "strategy_type" in err


def test_verify_raises_on_environment_mismatch(tmp_path):
    db_path = tmp_path / "env_mismatch.db"
    factory = _factory(db_path)
    factory.create_tables()
    _seed_full_parent_chain(factory, environment="testnet")

    with factory.get_session() as session:
        with pytest.raises(SharedDbParentError, match="environment"):
            verify_shared_db_parents(
                session,
                user_id=user_id_for("mainnet_live"),
                account_id=account_id_for("mainnet_live"),
                strategy_id=strategy_id_for("ltcusdt_test"),
                account_name="mainnet_live",
                strat_id="ltcusdt_test",
                primary_symbol="LTCUSDT",
                recorder_testnet=False,
            )


def test_prepare_fails_when_existing_environment_wrong(tmp_path, capsys):
    db_path = tmp_path / "env_main.db"
    factory = _factory(db_path)
    factory.create_tables()
    _seed_full_parent_chain(factory, environment="testnet")

    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path, testnet=False)
    gridbot_yaml = tmp_path / "gridbot.yaml"
    # Gridbot says mainnet so prepare passes the config-time check (config
    # parity) and falls through to verify (which catches the DB-row mismatch).
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path, account_testnet=False)

    rc = main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "environment" in err


# --- main() config-time guards --------------------------------------------


def test_bootstrap_fails_strat_id_not_in_gridbot_config(tmp_path, capsys):
    db_path = tmp_path / "strat_missing.db"
    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(
        recorder_yaml, db_path=db_path, strat_id="not_in_gridbot"
    )
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path)

    with pytest.raises(SystemExit) as exc:
        main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not_in_gridbot" in err
    assert "not found" in err


def test_bootstrap_fails_symbol_mismatch(tmp_path, capsys):
    db_path = tmp_path / "symbol.db"
    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path, symbols=["BTCUSDT"])
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path, symbol="LTCUSDT")

    with pytest.raises(SystemExit) as exc:
        main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "symbol" in err.lower()


def test_bootstrap_fails_recorder_testnet_mismatch(tmp_path, capsys):
    db_path = tmp_path / "testnet.db"
    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path, testnet=True)
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path, account_testnet=False)

    with pytest.raises(SystemExit) as exc:
        main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "testnet" in err


def test_prepare_fails_empty_symbols_with_account(tmp_path, capsys):
    db_path = tmp_path / "no_symbols.db"
    recorder_yaml = tmp_path / "recorder.yaml"
    _write_recorder_yaml(recorder_yaml, db_path=db_path, symbols=[])
    gridbot_yaml = tmp_path / "gridbot.yaml"
    _write_gridbot_yaml(gridbot_yaml, db_path=db_path)

    with pytest.raises(SystemExit) as exc:
        main([str(recorder_yaml), "--gridbot-config", str(gridbot_yaml)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "No symbols configured" in err
