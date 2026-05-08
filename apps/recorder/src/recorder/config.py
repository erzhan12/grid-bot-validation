"""Configuration models for data recorder.

Loads recorder configuration from YAML file with Pydantic validation.
"""

import os
import re
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator


class AccountConfig(BaseModel):
    """Optional exchange account for private stream capture."""

    api_key: SecretStr = Field(..., description="Bybit API key")
    api_secret: SecretStr = Field(..., description="Bybit API secret")


class RecorderConfig(BaseModel):
    """Root configuration for data recorder."""

    symbols: list[str] = Field(
        default_factory=list,
        description="Symbols to record (e.g., ['BTCUSDT'])",
    )

    @field_validator("symbols")
    @classmethod
    def non_empty_symbols(cls, v: list[str]) -> list[str]:
        if any(not s.strip() for s in v):
            raise ValueError("symbols must be non-empty, non-whitespace strings")
        return v

    capture_public_trades: bool = Field(
        default=False,
        description="Capture public trades (high volume, ~85%% of storage). "
        "Not needed for replay engine which uses ticker snapshots only.",
    )

    database_url: str = Field(
        default="sqlite:///recorder.db",
        description="SQLite database path",
    )
    testnet: bool = Field(
        default=False,
        description="Use testnet endpoints (default: mainnet)",
    )

    # Writer settings
    batch_size: int = Field(default=100, ge=1, description="Writer batch size")
    flush_interval: float = Field(
        default=5.0, gt=0, description="Writer flush interval in seconds"
    )

    # Gap reconciliation
    gap_threshold_seconds: float = Field(
        default=5.0, gt=0, description="Min gap to trigger REST reconciliation"
    )

    # Health monitoring
    health_log_interval: float = Field(
        default=300.0, gt=0, description="Seconds between health log lines"
    )

    # Optional private stream capture
    account: Optional[AccountConfig] = None


def load_config(config_path: Optional[str] = None) -> RecorderConfig:
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, checks:
            1. RECORDER_CONFIG_PATH environment variable
            2. conf/recorder.yaml
            3. recorder.yaml

    Returns:
        Validated RecorderConfig.

    Raises:
        FileNotFoundError: If no config file found.
        ValueError: If config validation fails.
    """
    if config_path is None:
        config_path = os.environ.get("RECORDER_CONFIG_PATH")

    if config_path is None:
        search_paths = [
            Path("conf/recorder.yaml"),
            Path("recorder.yaml"),
        ]
        for path in search_paths:
            if path.exists():
                config_path = str(path)
                break

    if config_path is None:
        raise FileNotFoundError(
            "No config file found. Set RECORDER_CONFIG_PATH or create conf/recorder.yaml"
        )

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Match gridbot's loader: load .env and expand ${VAR_NAME} placeholders
    # so the same `api_key: "${BYBIT_API_KEY}"` pattern works in both
    # gridbot.yaml and recorder.yaml.
    load_dotenv()

    try:
        with open(path) as f:
            raw = f.read()
    except OSError as e:
        raise ValueError(f"Cannot read config file {config_path}: {e}")

    def _expand_env(match: re.Match) -> str:
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            raise ValueError(
                f"Environment variable '{var_name}' not set "
                f"(referenced in {config_path})"
            )
        return value

    expanded = re.sub(r"\$\{(\w+)}", _expand_env, raw)

    try:
        data = yaml.safe_load(expanded)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {config_path}: {e}")

    if data is None:
        data = {}

    return RecorderConfig(**data)
