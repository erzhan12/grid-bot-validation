"""Shared utilities for database-related operations."""

from urllib.parse import urlparse, urlunparse


def redact_db_url(url: str) -> str:
    """Redact credentials from database URL for safe logging.

    Preserves scheme, username, host, port, and path — only the password
    is replaced with ``***``.  Returns the URL unchanged when no password
    is present (e.g. SQLite file paths).

    Example::

        >>> redact_db_url("postgresql://user:secret@host:5432/mydb")
        'postgresql://user:***@host:5432/mydb'
        >>> redact_db_url("sqlite:///recorder.db")
        'sqlite:///recorder.db'
    """
    parsed = urlparse(url)
    if not parsed.password:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=f"{parsed.username}:***@{host}"))
