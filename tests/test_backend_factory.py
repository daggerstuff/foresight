"""Tests for the backend factory (PIX-3983).

Verifies that ``create_backend()`` correctly routes based on
``FORESIGHT_DB_URL`` and falls back to ``SqliteBackend`` when no URL is set.
"""

from __future__ import annotations

import os

import pytest
from foresight_mcp.backend.backend_factory import create_backend
from foresight_mcp.backend.postgres_backend import PostgresBackend
from foresight_mcp.backend.sqlite_backend import SqliteBackend


class TestCreateBackend:
    def _clear_db_url(self, mp):
        """Clear both env var and factory module's DB_URL constant."""
        import foresight_mcp.backend.backend_factory as _factory

        mp.delenv("FORESIGHT_DB_URL", raising=False)
        _factory.DB_URL = ""

    def test_default_returns_sqlite_backend(self):
        """When neither FORESIGHT_DB_URL nor db_url is set, returns SqliteBackend."""
        with pytest.MonkeyPatch.context() as mp:
            self._clear_db_url(mp)
            backend = create_backend()
            assert isinstance(backend, SqliteBackend)

    def test_pg_url_returns_postgres_backend(self):
        """When db_url is a postgresql:// DSN, returns PostgresBackend."""
        backend = create_backend(db_url="postgresql://user:pass@localhost:5432/test")
        assert isinstance(backend, PostgresBackend)

    def test_explicit_db_url_overrides_env(self):
        """Explicit db_url argument takes precedence over FORESIGHT_DB_URL env."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("FORESIGHT_DB_URL", "postgresql://env:override@host/db")
            backend = create_backend(db_url="postgresql://explicit:override@other/db")
            assert isinstance(backend, PostgresBackend)
            assert "explicit:override" in backend._dsn

    def test_postgres_url_formats(self):
        """Both postgresql:// and postgres:// schemes are recognised."""
        for scheme in ("postgresql://", "postgres://"):
            url = f"{scheme}u:p@h/db"
            backend = create_backend(db_url=url)
            assert isinstance(backend, PostgresBackend)

    def test_empty_url_falls_back_to_sqlite(self):
        """An explicitly empty string falls back to SqliteBackend."""
        with pytest.MonkeyPatch.context() as mp:
            self._clear_db_url(mp)
            backend = create_backend(db_url="")
            assert isinstance(backend, SqliteBackend)

    def test_env_url_empty_string_falls_back_to_sqlite(self):
        """When FORESIGHT_DB_URL is set to empty string, falls back to SqliteBackend."""
        import foresight_mcp.backend.backend_factory as _factory

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("FORESIGHT_DB_URL", "")
            _factory.DB_URL = ""
            backend = create_backend()
            assert isinstance(backend, SqliteBackend)

    def test_backend_not_connected_after_create(self):
        """create_backend() returns an unconnected backend (caller must call connect())."""
        with pytest.MonkeyPatch.context() as mp:
            self._clear_db_url(mp)
            backend = create_backend()
            assert isinstance(backend, SqliteBackend)
            assert backend._pool is None
