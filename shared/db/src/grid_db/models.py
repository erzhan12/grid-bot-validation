"""SQLAlchemy ORM models for multi-tenant grid bot database.

Supports 7 tables:
- Core entities: users, bybit_accounts, api_credentials, strategies, runs
- Data tables: public_trades, private_executions
"""

from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional, List, Any
from uuid import uuid4

from sqlalchemy import (
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    Index,
    UniqueConstraint,
    JSON,
    BigInteger,
    Integer,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def generate_uuid() -> str:
    """Generate UUID string for primary keys."""
    return str(uuid4())


def utc_now() -> datetime:
    """Get current UTC timestamp."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class User(Base):
    """User account for multi-tenant access control."""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(
        String(20), default="active"
    )  # active, suspended, deleted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    # Relationships
    accounts: Mapped[List["BybitAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    runs: Mapped[List["Run"]] = relationship(
        back_populates="user"
    )


class BybitAccount(Base):
    """Bybit exchange account linked to a user."""

    __tablename__ = "bybit_accounts"

    account_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    account_name: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str] = mapped_column(
        String(10), nullable=False
    )  # 'mainnet' or 'testnet'
    status: Mapped[str] = mapped_column(
        String(20), default="enabled"
    )  # enabled, disabled, error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="accounts")
    credentials: Mapped[List["ApiCredential"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    strategies: Mapped[List["Strategy"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    runs: Mapped[List["Run"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "account_name", name="uq_user_account_name"),
    )


class ApiCredential(Base):
    """API credentials for a Bybit account.

    Note: api_secret is stored as plaintext for initial implementation.
    Encryption will be added in a future phase.
    """

    __tablename__ = "api_credentials"

    credential_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("bybit_accounts.account_id", ondelete="CASCADE"),
        nullable=False,
    )
    api_key_id: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # Public API key
    api_secret: Mapped[str] = mapped_column(Text, nullable=False)  # Plaintext for now
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    account: Mapped["BybitAccount"] = relationship(back_populates="credentials")


class Strategy(Base):
    """Grid trading strategy configuration for an account."""

    __tablename__ = "strategies"

    strategy_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("bybit_accounts.account_id", ondelete="CASCADE"),
        nullable=False,
    )
    strategy_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # 'GridStrategy'
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)  # e.g., 'BTCUSDT'
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )  # GridConfig as JSON
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    # Relationships
    account: Mapped["BybitAccount"] = relationship(back_populates="strategies")
    runs: Mapped[List["Run"]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("account_id", "symbol", name="uq_account_symbol"),
    )


class Run(Base):
    """Execution run for live trading, backtesting, or shadow mode."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("bybit_accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    strategy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("strategies.strategy_id", ondelete="CASCADE"), nullable=False
    )
    run_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # 'live', 'backtest', 'shadow'
    gridcore_version: Mapped[Optional[str]] = mapped_column(String(50))
    config_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    end_ts: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(20), default="running"
    )  # running, completed, failed

    # Relationships
    user: Mapped["User"] = relationship(back_populates="runs")
    account: Mapped["BybitAccount"] = relationship(back_populates="runs")
    strategy: Mapped["Strategy"] = relationship(back_populates="runs")
    executions: Mapped[List["PrivateExecution"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_runs_user_id", "user_id"),
        Index("ix_runs_account_id", "account_id"),
        Index("ix_runs_status", "status"),
    )


class PublicTrade(Base):
    """Public trade data for trade-through fill simulation."""

    __tablename__ = "public_trades"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    trade_id: Mapped[str] = mapped_column(String(50), nullable=False)
    exchange_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # 'Buy' or 'Sell'
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    size: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)

    __table_args__ = (
        Index("ix_public_trades_symbol_exchange_ts", "symbol", "exchange_ts"),
    )


class PrivateExecution(Base):
    """Private execution data - ground truth for validation."""

    __tablename__ = "private_executions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    exec_id: Mapped[str] = mapped_column(String(50), nullable=False)
    order_id: Mapped[str] = mapped_column(String(50), nullable=False)
    order_link_id: Mapped[Optional[str]] = mapped_column(
        String(50)
    )  # Client order ID for matching
    exchange_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # 'Buy' or 'Sell'
    exec_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    exec_qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    exec_fee: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    closed_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    raw_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    # Relationships
    run: Mapped["Run"] = relationship(back_populates="executions")

    __table_args__ = (
        Index("ix_private_executions_account_exchange_ts", "account_id", "exchange_ts"),
        Index("ix_private_executions_run_id", "run_id"),
    )
