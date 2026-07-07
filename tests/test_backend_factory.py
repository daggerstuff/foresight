"""Tests for the backend factory (PIX-3983).

Verifies that ``create_backend()`` correctly routes based on
``FORESIGHT_DB_URL`` and fails when no Postgres DSN is provided.
"""

from __future__ import annotations

import os

import pytest
from foresight.backend.backend_factory import create_backend
from foresight.backend.postgres_backend import PostgresBackend


class TestCreateBackend:
    def _clear_db_url(self, mp):
        """Clear both env var and factory module's DB_URL constant."""
        import foresight.backend.backend_factory as _factory

        mp.delenv("FORESIGHT_DB_URL", raising=False)
        _factory.DB_URL = ""

    def test_default_raises_runtime_error(self):
        """When neither FORESIGHT_DB_URL nor db_url is set, raises RuntimeError."""
        with pytest.MonkeyPatch.context() as mp:
            self._clear_db_url(mp)
            with pytest.raises(RuntimeError, match="FORESIGHT_DB_URL"):
                create_backend()

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

    def test_empty_url_raises_runtime_error(self):
        """An explicitly empty string raises RuntimeError."""
        with pytest.MonkeyPatch.context() as mp:
            self._clear_db_url(mp)
            with pytest.raises(RuntimeError, match="FORESIGHT_DB_URL"):
                create_backend(db_url="")

    def test_env_url_empty_string_raises_runtime_error(self):
        """When FORESIGHT_DB_URL is set to empty string, raises RuntimeError."""
        import foresight.backend.backend_factory as _factory

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("FORESIGHT_DB_URL", "")
            _factory.DB_URL = ""
            with pytest.raises(RuntimeError, match="FORESIGHT_DB_URL"):
                create_backend()

    def test_create_backend_requires_url(self):
        """The error message tells the user to set FORESIGHT_DB_URL."""
        import foresight.backend.backend_factory as _factory

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("FORESIGHT_DB_URL", raising=False)
            _factory.DB_URL = ""
            with pytest.raises(RuntimeError) as exc_info:
                create_backend()
            msg = str(exc_info.value)
            assert "FORESIGHT_DB_URL" in msg
            assert "postgresql://" in msg
            assert "SQLite" in msg
