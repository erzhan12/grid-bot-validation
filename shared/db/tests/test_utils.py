"""Tests for grid_db.utils."""

from grid_db.utils import redact_db_url


class TestRedactDbUrl:
    """Tests for redact_db_url()."""

    def test_sqlite_file_path_unchanged(self):
        url = "sqlite:///recorder.db"
        assert redact_db_url(url) == url

    def test_sqlite_memory_unchanged(self):
        url = "sqlite:///:memory:"
        assert redact_db_url(url) == url

    def test_postgresql_with_password_redacted(self):
        url = "postgresql://user:secret@host:5432/mydb"
        assert redact_db_url(url) == "postgresql://user:***@host:5432/mydb"

    def test_postgresql_without_password_unchanged(self):
        url = "postgresql://user@host:5432/mydb"
        assert redact_db_url(url) == url

    def test_special_chars_in_password(self):
        url = "postgresql://user:p%40ss%23word!@host:5432/mydb"
        result = redact_db_url(url)
        assert "***" in result
        assert "p%40ss" not in result
        assert "word!" not in result

    def test_no_port(self):
        url = "postgresql://user:secret@host/mydb"
        assert redact_db_url(url) == "postgresql://user:***@host/mydb"

    def test_ipv6_host(self):
        url = "postgresql://user:secret@[::1]:5432/mydb"
        result = redact_db_url(url)
        assert "secret" not in result
        assert "***" in result
        assert "::1" in result

    def test_empty_string(self):
        assert redact_db_url("") == ""

    def test_mysql_scheme(self):
        url = "mysql://admin:hunter2@db.example.com:3306/app"
        result = redact_db_url(url)
        assert result == "mysql://admin:***@db.example.com:3306/app"
