"""Shared fixtures for pnl_checker tests."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_credential_env_vars(monkeypatch):
    """Prevent BYBIT_API_KEY/BYBIT_API_SECRET from leaking into tests.

    AccountConfig.apply_env_overrides reads these env vars on every
    construction. Without isolation, tests would silently pick up
    real credentials from CI/CD or developer environments.
    """
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
