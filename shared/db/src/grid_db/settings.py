"""Database configuration with dual-database support (SQLite/PostgreSQL)."""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database configuration supporting SQLite (dev) and PostgreSQL (prod).

    Configuration can be provided via:
    - Direct database_url parameter
    - Individual components (db_type, db_host, etc.)
    - Environment variables with GRIDBOT_DB_ prefix
    """

    # Direct URL override (preferred)
    database_url: Optional[str] = None

    # Individual components (fallback)
    db_type: str = "sqlite"  # 'sqlite' or 'postgresql'
    db_host: Optional[str] = None
    db_port: Optional[str] = None
    db_user: Optional[str] = None
    db_password: Optional[str] = None
    db_name: str = "grid_bot.db"

    # Connection pool settings (PostgreSQL only)
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout: int = 30
    pool_recycle: int = 1800

    # Debug
    echo_sql: bool = False

    model_config = SettingsConfigDict(env_prefix="GRIDBOT_", env_file=".env")

    def get_database_url(self) -> str:
        """Build database URL from settings.

        Returns:
            SQLAlchemy-compatible database URL string.

        Raises:
            ValueError: If PostgreSQL is selected but required fields are missing.
        """
        if self.database_url:
            return self.database_url

        if self.db_type == "sqlite":
            return f"sqlite+pysqlite:///{self.db_name}"

        if self.db_type == "postgresql":
            if not all([self.db_host, self.db_port, self.db_user, self.db_password]):
                raise ValueError(
                    "PostgreSQL requires db_host, db_port, db_user, and db_password"
                )
            return (
                f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )

        raise ValueError(f"Unsupported database type: {self.db_type}")
