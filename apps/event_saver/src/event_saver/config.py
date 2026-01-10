"""Configuration for event saver service."""

from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_symbols_string(v: str | None) -> list[str]:
    """Parse comma-separated symbols string to list."""
    if v is None or v == "":
        return []
    return [s.strip().upper() for s in v.split(",") if s.strip()]


class EventSaverConfig(BaseSettings):
    """Configuration for event saver service.

    Environment variables:
    - EVENTSAVER_SYMBOLS: Comma-separated symbol list (default: empty)
    - EVENTSAVER_TESTNET: Use testnet endpoints (default: true)
    - EVENTSAVER_BATCH_SIZE: Trades to batch before bulk insert (default: 100)
    - EVENTSAVER_FLUSH_INTERVAL: Seconds between forced flushes (default: 5.0)
    - EVENTSAVER_GAP_THRESHOLD_SECONDS: Seconds to trigger reconciliation (default: 5.0)
    - EVENTSAVER_DATABASE_URL: Database connection URL (default: sqlite:///gridbot.db)
    """

    # Symbols to capture as comma-separated string
    symbols: str = ""

    # Environment
    testnet: bool = True

    # Writer settings
    batch_size: int = 100  # Trades to batch before bulk insert
    flush_interval: float = 5.0  # Max seconds between flushes

    # Gap detection
    gap_threshold_seconds: float = 5.0  # Trigger reconciliation if gap > this

    # Database URL
    database_url: str = "sqlite:///gridbot.db"

    model_config = SettingsConfigDict(
        env_prefix="EVENTSAVER_",
        env_file=".env",
        extra="ignore",
    )

    def get_symbols(self) -> list[str]:
        """Get symbols as a list."""
        return parse_symbols_string(self.symbols)
