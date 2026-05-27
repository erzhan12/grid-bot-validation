"""Pin uuid5 outputs so any drift in formula or namespace fails loudly."""

from grid_db.identity import (
    UUID_NAMESPACE,
    account_id_for,
    strategy_id_for,
    user_id_for,
)


def test_uuid_namespace_value() -> None:
    assert str(UUID_NAMESPACE) == "12345678-1234-5678-1234-567812345678"


def test_account_id_for_mainnet_live() -> None:
    assert account_id_for("mainnet_live") == "9bdb9748-f9e0-5c13-b144-0ad6a8dbcaba"


def test_account_id_for_other_name() -> None:
    assert account_id_for("other") == "33dd47cb-1f5f-586f-8b67-3375290c079d"


def test_user_id_for_mainnet_live() -> None:
    assert user_id_for("mainnet_live") == "33cbde62-dd96-5f63-83f4-c5de9b751f42"


def test_user_id_for_other_name() -> None:
    assert user_id_for("other") == "782a2a76-a319-5950-99a5-6a6213f03494"


def test_strategy_id_for_ltcusdt_test() -> None:
    assert strategy_id_for("ltcusdt_test") == "c10b8a88-323d-5daa-8216-de8b0bc2bf3c"


def test_strategy_id_for_other_strat() -> None:
    assert strategy_id_for("other") == "fcc67762-6d42-520f-a237-b8e60a50ac40"
