"""Tests for database initialization script."""

import sys
from unittest.mock import patch, MagicMock

from grid_db.init_db import main, initialize_database
from grid_db.settings import DatabaseSettings


class TestInitDb:
    """Tests for init_db module."""

    def test_initialize_database(self, db_settings):
        """initialize_database creates tables."""
        db = initialize_database(db_settings)
        
        # Verify tables exist
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        assert "users" in tables
        assert "runs" in tables

    def test_main_success(self, db_settings, monkeypatch, capsys):
        """Main entry point runs successfully."""
        # Mock sys.argv
        test_args = ["init_db.py", "--db-type", "sqlite", "--db-name", ":memory:"]
        monkeypatch.setattr(sys, "argv", test_args)

        # Run main
        ret = main()
        
        assert ret == 0
        captured = capsys.readouterr()
        assert "Database initialized successfully" in captured.out
        assert "Tables created" in captured.out
        assert "users" in captured.out

    def test_main_error_handling(self, monkeypatch, capsys):
        """Main handles errors gracefully."""
        # Mock initialize_database to raise exception
        with patch("grid_db.init_db.initialize_database") as mock_init:
            mock_init.side_effect = Exception("Connection failed")
            
            test_args = ["init_db.py"]
            monkeypatch.setattr(sys, "argv", test_args)
            
            ret = main()
            
            assert ret == 1
            captured = capsys.readouterr()
            assert "Error initializing database: Connection failed" in captured.err

