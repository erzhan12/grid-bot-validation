from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import QueuePool

from config.settings import DatabaseSettings

# Load settings once
_settings = DatabaseSettings()

# Create a database engine (SQLite in-memory ignores pooling args, but safe)
engine = create_engine(
    _settings.database_url,
    echo=_settings.echo_sql,
    poolclass=QueuePool,
    pool_recycle=1800,  # 30 minutes
    pool_size=5,  # 5 connections
    max_overflow=10,  # 10 additional connections
    pool_timeout=30,  # 30 seconds
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Base class for all models
class Base(DeclarativeBase):
    pass


# Function to get a database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
