"""A failed write must not leave the shared SQLite connection mid-transaction.

Issue #425: ``create_channel`` caught ``IntegrityError`` and re-raised
``ValueError`` without rollback, so the failed INSERT's implicit transaction
stayed open.  Every other connection to the same file then blocked on its
next write.  These tests use a second connection with a short timeout, so a
regression fails immediately instead of hanging.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from mansio_core import Bus, SQLiteBackend
from mansio_core.types import ACLEntry, ChannelMeta, Message

_CREATED = "2026-01-01T00:00:00+00:00"


def _meta(name: str, owner: str = "alice") -> ChannelMeta:
    return ChannelMeta(name=name, owner=owner, visibility="public", created_at=_CREATED)


def _write_from_another_connection(path) -> None:
    """Write through a second connection.

    Raises ``sqlite3.OperationalError: database is locked`` when the backend
    still holds a write transaction, after the short timeout.
    """
    other = sqlite3.connect(str(path), timeout=0.2)
    try:
        other.execute(
            "INSERT INTO channels (name, owner, visibility, created_at) VALUES (?, ?, ?, ?)",
            ("other", "bob", "public", _CREATED),
        )
        other.commit()
    finally:
        other.close()


def test_duplicate_create_channel_rolls_back(tmp_path) -> None:
    path = tmp_path / "bus.db"
    backend = SQLiteBackend(str(path))
    try:
        backend.create_channel(_meta("dup"), [ACLEntry(channel="dup", user_id="alice")])

        with pytest.raises(ValueError, match="already exists"):
            backend.create_channel(_meta("dup"), [ACLEntry(channel="dup", user_id="mallory")])

        # The failed INSERT left no transaction behind.
        _write_from_another_connection(path)
        # And its ACL rows were not written either.
        assert [entry.user_id for entry in backend.get_acl("dup")] == ["alice"]
    finally:
        backend.close()


def test_store_rolls_back_when_the_insert_is_rejected(tmp_path) -> None:
    path = tmp_path / "bus.db"
    backend = SQLiteBackend(str(path))
    message = Message(
        id="m-1",
        channel="chan",
        sender="alice",
        msg_type="text",
        payload="hello",
        timestamp=_CREATED,
    )
    try:
        backend.store(message)
        with pytest.raises(sqlite3.IntegrityError):
            backend.store(message)
        _write_from_another_connection(path)
    finally:
        backend.close()


def test_compact_that_removes_nothing_ends_its_transaction(tmp_path) -> None:
    path = tmp_path / "bus.db"
    backend = SQLiteBackend(str(path))
    try:
        backend.store(
            Message(
                id="m-1",
                channel="chan",
                sender="alice",
                msg_type="text",
                payload="hello",
                timestamp=_CREATED,
            )
        )

        # A DELETE opens a write transaction even when it removes no rows.
        assert backend.compact("chan", max_messages=10) == 0
        _write_from_another_connection(path)
    finally:
        backend.close()


def test_bus_duplicate_channel_propagates_the_same_error(tmp_path) -> None:
    """Behavior parity: the public API still raises the same ValueError."""
    bus = Bus(backend=SQLiteBackend(str(tmp_path / "bus.db")))
    try:
        bus.create_channel("dup", "alice")
        with pytest.raises(ValueError, match="Channel 'dup' already exists"):
            bus.create_channel("dup", "bob")
    finally:
        bus.close()


def test_acl_failure_propagates_as_itself_and_rolls_back(tmp_path) -> None:
    """Only the channel INSERT means "already exists"; an ACL failure is its own."""
    path = tmp_path / "bus.db"
    backend = SQLiteBackend(str(path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            backend.create_channel(
                _meta("fresh"),
                # user_id is NOT NULL; make the ACL insert fail on its own.
                [ACLEntry(channel="fresh", user_id=None)],  # type: ignore[arg-type]
            )
        assert backend.get_channel("fresh") is None
        _write_from_another_connection(path)
    finally:
        backend.close()


def test_update_channel_with_no_fields_does_not_deadlock(tmp_path) -> None:
    """The empty-update path must not re-enter the non-reentrant lock."""
    backend = SQLiteBackend(str(tmp_path / "bus.db"))
    try:
        backend.create_channel(_meta("chan"))
        result: dict[str, bool] = {}

        def call() -> None:
            result["exists"] = backend.update_channel("chan")

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "update_channel(name) deadlocked on self._lock"
        assert result["exists"] is True
        assert backend.update_channel("missing") is False
    finally:
        backend.close()
