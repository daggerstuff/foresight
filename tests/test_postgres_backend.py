"""Unit tests for PostgresBackend dialect translation (PIX-3983).

These tests verify the SQLite-to-PostgreSQL translation layer and
connection helpers WITHOUT a real PostgreSQL server — they only
exercise the pure-Python transformation functions.
"""

from __future__ import annotations

import pytest
from foresight.backend.postgres_backend import (
    PostgresBackend,
    _translate_sql,
)


class TestTranslateSql:
    """Tests for _translate_sql — SQLite → PostgreSQL dialect mapping."""

    def test_placeholder_translation(self):
        """Question-mark placeholders become %s."""
        sql = "SELECT * FROM t WHERE id = ? AND name = ?"
        assert _translate_sql(sql) == "SELECT * FROM t WHERE id = %s AND name = %s"

    def test_escaped_question_mark_preserved(self):
        """SQLite LIKE '?' pattern — percent-escaped ? is preserved."""
        sql = "SELECT * FROM t WHERE val LIKE '?%'"
        result = _translate_sql(sql)
        assert "?%" in result

    def test_autoincrement_to_serial(self):
        """INTEGER PRIMARY KEY AUTOINCREMENT becomes SERIAL."""
        sql = "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)"
        expected = "CREATE TABLE t (id SERIAL, name TEXT)"
        assert _translate_sql(sql) == expected

    def test_autoincrement_case_insensitive(self):
        """Case-insensitive matching of AUTOINCREMENT."""
        sql = "id integer primary key autoincrement"
        assert _translate_sql(sql) == "id SERIAL"

    def test_blob_to_bytea(self):
        """BLOB column type becomes BYTEA."""
        sql = "CREATE TABLE t (data BLOB)"
        assert _translate_sql(sql) == "CREATE TABLE t (data BYTEA)"

    def test_blob_case_insensitive(self):
        """Case-insensitive matching of BLOB."""
        sql = "CREATE TABLE t (data blob)"
        assert _translate_sql(sql) == "CREATE TABLE t (data BYTEA)"

    def test_combined_translation(self):
        """All transformations applied together."""
        sql = (
            "CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "data BLOB, tenant_id TEXT NOT NULL DEFAULT 'default', "
            "user_id TEXT NOT NULL, content TEXT)"
        )
        result = _translate_sql(sql)
        assert "id SERIAL" in result
        assert "data BYTEA" in result
        assert "?" not in result  # No placeholders
        # All ? should have been translated — verify none left in positional spots
        for line in result.split(","):
            assert " ?" not in line

    def test_no_translation_needed(self):
        """SQL with no SQLite-specific syntax passes through unchanged."""
        sql = "SELECT id, name FROM users WHERE active = %s ORDER BY name"
        assert _translate_sql(sql) == sql

    def test_insert_with_question_marks(self):
        """INSERT with positional placeholders."""
        sql = "INSERT INTO t (a, b, c) VALUES (?, ?, ?)"
        assert _translate_sql(sql) == "INSERT INTO t (a, b, c) VALUES (%s, %s, %s)"


class TestPostgresBackendHelpers:
    """Tests for PostgresBackend static/private helpers."""

    def test_ensure_sslmode_appends_when_missing(self):
        """_ensure_sslmode appends sslmode=require when no sslmode present."""
        dsn = PostgresBackend._ensure_sslmode("postgresql://user:pass@host/db")
        assert "sslmode=require" in dsn

    def test_ensure_sslmode_uses_and_when_query_present(self):
        """_ensure_sslmode uses & separator when ? already present."""
        dsn = PostgresBackend._ensure_sslmode("postgresql://user:pass@host/db?connect_timeout=10")
        assert "connect_timeout=10&sslmode=require" in dsn

    def test_ensure_sslmode_preserves_existing(self):
        """_ensure_sslmode does not duplicate sslmode when already set."""
        dsn = PostgresBackend._ensure_sslmode("postgresql://user:pass@host/db?sslmode=disable")
        assert dsn == "postgresql://user:pass@host/db?sslmode=disable"
        assert dsn.count("sslmode") == 1

    def test_redact_dsn_hides_password(self):
        """_redact_dsn replaces password with ****."""
        backend = PostgresBackend(dsn="postgresql://user:secret123@host:5432/db")
        redacted = backend._redact_dsn()
        assert "secret123" not in redacted
        assert "user:****@" in redacted

    def test_redact_dsn_no_password(self):
        """_redact_dsn handles DSNs without a password."""
        backend = PostgresBackend(dsn="postgresql://user@host/db")
        redacted = backend._redact_dsn()
        assert "****" not in redacted

    def test_backend_type(self):
        """PostgresBackend reports backend_type correctly."""
        backend = PostgresBackend(dsn="postgresql://u:p@h/db")
        # backend_type is set after connect(); default is None
        assert backend.backend_type is None

    def test_unconnected_exception_message(self):
        """Unconnected PostgresBackend raises clear RuntimeError."""
        backend = PostgresBackend(dsn="postgresql://u:p@h/db")
        with pytest.raises(RuntimeError, match="PostgresBackend not connected"), backend.connection():
            pass

    def test_stats_before_connect(self):
        """Stats returns zeros before connect()."""
        backend = PostgresBackend(dsn="postgresql://u:p@h/db")
        stats = backend.stats
        assert stats["idle"] == 0
        assert stats["in_use"] == 0
        assert stats["max_size"] == 20
