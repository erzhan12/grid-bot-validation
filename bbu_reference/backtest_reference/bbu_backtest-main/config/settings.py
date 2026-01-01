import os
from typing import List, Optional

import yaml
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class PairTimeframe(BaseSettings):
    id: int
    strat: str
    symbol: str
    greed_step: float = 0.3
    max_margin: int = 5
    greed_count: int = 40
    min_liq_ratio: float = 0.8
    max_liq_ratio: float = 1.2
    exchange: str = 'bybit'
    long_koef: float = 1.0
    min_total_margin: int = 0


class Amount(BaseSettings):
    name: str
    amount: str  # e.g., 'x0.005'
    strat: int

    @property
    def numeric_amount(self) -> float:
        return float(self.amount.lstrip('x'))
    

class ConfigData(BaseSettings):
    pair_timeframes: List[PairTimeframe]
    amounts: List[Amount]


class DatabaseSettings(BaseSettings):
    """Application settings with safe defaults and DATABASE_URL override support."""

    # Optional components (used if DATABASE_URL not provided)
    data_dir: Optional[str] = os.getenv("DATA_DIR")
    db_type: Optional[str] = os.getenv("DB_TYPE")
    db_host: Optional[str] = os.getenv("DB_HOST")
    db_port: Optional[str] = os.getenv("DB_PORT")
    db_user: Optional[str] = os.getenv("DB_USER")
    db_password: Optional[str] = os.getenv("DB_PASSWORD")
    db_name: Optional[str] = os.getenv("DB_NAME")

    # Direct URL override (preferred)
    _database_url_env: Optional[str] = os.getenv("DATABASE_URL")

    # SQL echo logging, defaults to false if not set
    echo_sql: bool = (os.getenv("ECHO_SQL") or "false").lower() in ["true", "1", "t", "yes", "y"]

    model_config = SettingsConfigDict(env_file=".env")

    @property
    def database_url(self) -> str:
        """Resolve database URL from env or composed parts.

        Prefers DATABASE_URL if provided; otherwise, composes from DB_* parts.
        Provides sane handling for SQLite (including in-memory).
        """
        if self._database_url_env:
            return self._database_url_env

        # Fallback: compose from parts
        if self.db_type and self.db_type.startswith("sqlite"):
            # SQLite - allow file path or :memory:
            name = self.db_name or ":memory:"
            # Ensure three slashes for absolute/relative paths
            return f"sqlite+pysqlite:///{name}"

        required_parts = [self.db_type, self.db_user, self.db_password, self.db_host, self.db_port, self.db_name]
        if all(required_parts):
            return (
                f"{self.db_type}://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
            )

        raise ValueError(
            "Incomplete database configuration. Set DATABASE_URL or all of DB_TYPE, DB_HOST, DB_PORT, "
            "DB_USER, DB_PASSWORD, DB_NAME."
        )


class Settings:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.yaml_data = None
            cls._instance.amounts = []
            cls._instance.pair_timeframes = []
        return cls._instance

    def read_settings(self):
        self.init()

    def init(self):
        self.yaml_data = self.__read_yaml()

        self.amounts = self.yaml_data.amounts

        self.pair_timeframes = self.yaml_data.pair_timeframes

    def __read_yaml(self):
        data = self.__load_yaml_file([
            'config/config.yaml',
            '../config/config.yaml',
            # '/opt/bbb3/conf/config.yaml'
        ])
        return ConfigData(**data)

    @staticmethod
    def __load_yaml_file(file_paths):
        for path in file_paths:
            real_path = os.path.realpath(path)
            if os.path.exists(real_path):
                with open(real_path, 'r') as stream:
                    data = yaml.safe_load(stream)
                    return data
        raise FileNotFoundError(f"None of the files found in {file_paths}")


settings = Settings()
settings.read_settings()