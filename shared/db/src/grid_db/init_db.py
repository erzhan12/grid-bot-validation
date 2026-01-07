"""Database initialization script.

Usage:
    python -m grid_db.init_db

Environment variables:
    GRIDBOT_DB_TYPE: 'sqlite' (default) or 'postgresql'
    GRIDBOT_DB_NAME: Database name or file path
    GRIDBOT_DB_HOST, GRIDBOT_DB_PORT, GRIDBOT_DB_USER, GRIDBOT_DB_PASSWORD: PostgreSQL config
"""

import argparse
import sys

from sqlalchemy import inspect

from grid_db.settings import DatabaseSettings
from grid_db.database import DatabaseFactory


def initialize_database(settings: DatabaseSettings = None) -> DatabaseFactory:
    """Initialize database and create all tables.

    Args:
        settings: Database configuration. Uses defaults/env vars if not provided.

    Returns:
        Configured DatabaseFactory instance.
    """
    settings = settings or DatabaseSettings()
    db = DatabaseFactory(settings)
    db.create_tables()
    return db


def main():
    """CLI entry point for database initialization."""
    parser = argparse.ArgumentParser(
        description="Initialize grid-bot-validation database"
    )
    parser.add_argument(
        "--db-type",
        choices=["sqlite", "postgresql"],
        help="Database type (default: sqlite)",
    )
    parser.add_argument(
        "--db-name",
        help="Database name or file path",
    )
    parser.add_argument(
        "--echo-sql",
        action="store_true",
        help="Echo SQL statements (debug mode)",
    )
    args = parser.parse_args()

    # Build settings from args + env
    settings_kwargs = {}
    if args.db_type:
        settings_kwargs["db_type"] = args.db_type
    if args.db_name:
        settings_kwargs["db_name"] = args.db_name
    if args.echo_sql:
        settings_kwargs["echo_sql"] = True

    settings = DatabaseSettings(**settings_kwargs)

    print(f"Initializing database: {settings.get_database_url()}")

    try:
        db = initialize_database(settings)
        print("Database initialized successfully.")
        print("Tables created:")
        inspector = inspect(db.engine)
        for table in inspector.get_table_names():
            print(f"  - {table}")
        return 0
    except Exception as e:
        print(f"Error initializing database: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
