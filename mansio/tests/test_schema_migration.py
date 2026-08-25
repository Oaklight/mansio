"""Tests for schema versioning and migration protection (#112)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mansio.backends.sqlite import (
    SCHEMA_VERSION,
    SchemaVersionError,
    _get_schema_version,
    _has_table,
    _set_schema_version,
    backup_database,
    check_schema,
)

# ── helpers ───────────────────────────────────────────────────────


def _fresh_conn(db_path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_legacy_db(conn: sqlite3.Connection) -> None:
    """Create tables as they existed before schema versioning."""
    conn.executescript(
        """\
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            sender TEXT NOT NULL,
            msg_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            metadata TEXT
        );
        CREATE TABLE agent_presence (
            agent_id TEXT PRIMARY KEY,
            last_seen TEXT NOT NULL,
            metadata TEXT
        );
        """
    )
    conn.commit()


# ── _get_schema_version / _set_schema_version ────────────────────


class TestSchemaVersionHelpers:
    def test_no_meta_table_returns_none(self) -> None:
        conn = _fresh_conn()
        assert _get_schema_version(conn) is None

    def test_empty_meta_table_returns_none(self) -> None:
        conn = _fresh_conn()
        conn.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        assert _get_schema_version(conn) is None

    def test_set_and_get(self) -> None:
        conn = _fresh_conn()
        _set_schema_version(conn, 42)
        assert _get_schema_version(conn) == 42

    def test_set_overwrites(self) -> None:
        conn = _fresh_conn()
        _set_schema_version(conn, 1)
        _set_schema_version(conn, 2)
        assert _get_schema_version(conn) == 2


# ── _has_table ────────────────────────────────────────────────────


class TestHasTable:
    def test_missing_table(self) -> None:
        conn = _fresh_conn()
        assert _has_table(conn, "messages") is False

    def test_existing_table(self) -> None:
        conn = _fresh_conn()
        conn.execute("CREATE TABLE messages (id TEXT)")
        assert _has_table(conn, "messages") is True


# ── check_schema ──────────────────────────────────────────────────


class TestCheckSchema:
    def test_fresh_database(self) -> None:
        conn = _fresh_conn()
        result = check_schema(conn, ":memory:")
        assert result == "fresh"

    def test_legacy_database_gets_stamped(self) -> None:
        conn = _fresh_conn()
        _create_legacy_db(conn)
        result = check_schema(conn, ":memory:")
        assert result == "legacy"
        assert _get_schema_version(conn) == SCHEMA_VERSION

    def test_current_version_passes(self) -> None:
        conn = _fresh_conn()
        _create_legacy_db(conn)
        _set_schema_version(conn, SCHEMA_VERSION)
        conn.commit()
        result = check_schema(conn, ":memory:")
        assert result == "current"

    def test_future_version_raises(self) -> None:
        conn = _fresh_conn()
        _create_legacy_db(conn)
        _set_schema_version(conn, SCHEMA_VERSION + 1)
        conn.commit()
        with pytest.raises(SchemaVersionError, match="schema version"):
            check_schema(conn, ":memory:")

    def test_future_version_error_message(self) -> None:
        conn = _fresh_conn()
        _create_legacy_db(conn)
        future = SCHEMA_VERSION + 5
        _set_schema_version(conn, future)
        conn.commit()
        with pytest.raises(SchemaVersionError) as exc_info:
            check_schema(conn, "test.db")
        assert str(future) in str(exc_info.value)
        assert "Upgrade mansio" in str(exc_info.value)


# ── backup_database ──────────────────────────────────────────────


class TestBackupDatabase:
    def test_backup_creates_file(self, tmp_path: Path) -> None:
        db_file = tmp_path / "test.db"
        db_file.write_text("dummy data")
        backup_path = backup_database(db_file)
        assert backup_path is not None
        assert backup_path.exists()
        assert backup_path.read_text() == "dummy data"
        assert ".db.bak." in str(backup_path)

    def test_backup_nonexistent_returns_none(self, tmp_path: Path) -> None:
        result = backup_database(tmp_path / "missing.db")
        assert result is None

    def test_backup_memory_returns_none(self) -> None:
        result = backup_database(":memory:")
        assert result is None

    def test_backup_preserves_content(self, tmp_path: Path) -> None:
        db_file = tmp_path / "real.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('hello')")
        conn.commit()
        conn.close()

        backup_path = backup_database(db_file)
        assert backup_path is not None
        bconn = sqlite3.connect(str(backup_path))
        rows = bconn.execute("SELECT x FROM t").fetchall()
        assert rows == [("hello",)]
        bconn.close()


# ── SQLiteBackend integration ────────────────────────────────────


class TestSQLiteBackendSchemaIntegration:
    def test_fresh_init_sets_version(self) -> None:
        from mansio.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(":memory:")
        version = _get_schema_version(backend._conn)
        assert version == SCHEMA_VERSION
        backend.close()

    def test_fresh_init_creates_meta_table(self) -> None:
        from mansio.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(":memory:")
        assert _has_table(backend._conn, "_meta")
        backend.close()

    def test_reopen_existing_db(self, tmp_path: Path) -> None:
        from mansio.backends.sqlite import SQLiteBackend

        db_file = tmp_path / "test.db"
        b1 = SQLiteBackend(str(db_file))
        b1.close()

        # Reopen — should detect "current" and succeed
        b2 = SQLiteBackend(str(db_file))
        assert _get_schema_version(b2._conn) == SCHEMA_VERSION
        b2.close()

    def test_future_version_rejects(self, tmp_path: Path) -> None:
        from mansio.backends.sqlite import SQLiteBackend

        db_file = tmp_path / "future.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, channel TEXT, "
            "sender TEXT, msg_type TEXT, payload TEXT, timestamp TEXT)"
        )
        _set_schema_version(conn, SCHEMA_VERSION + 10)
        conn.commit()
        conn.close()

        with pytest.raises(SchemaVersionError):
            SQLiteBackend(str(db_file))

    def test_legacy_db_gets_adopted(self, tmp_path: Path) -> None:
        from mansio.backends.sqlite import SQLiteBackend

        db_file = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_file))
        _create_legacy_db(conn)
        conn.close()

        backend = SQLiteBackend(str(db_file))
        assert _get_schema_version(backend._conn) == SCHEMA_VERSION
        backend.close()


# ── SQLiteBus integration ────────────────────────────────────────


class TestSQLiteBusSchemaIntegration:
    def test_bus_passes_force_init(self) -> None:
        from mansio.bus import SQLiteBus

        bus = SQLiteBus(":memory:", force_init=True)
        assert _get_schema_version(bus._backend._conn) == SCHEMA_VERSION
        bus.close()

    def test_bus_future_version_rejects(self, tmp_path: Path) -> None:
        from mansio.bus import SQLiteBus

        db_file = tmp_path / "future.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY)")
        _set_schema_version(conn, SCHEMA_VERSION + 1)
        conn.commit()
        conn.close()

        with pytest.raises(SchemaVersionError):
            SQLiteBus(str(db_file))
