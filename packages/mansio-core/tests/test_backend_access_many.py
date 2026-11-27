"""Batch channel-access checks: parity, expectations, chunking, non-store backends."""

from __future__ import annotations

import math

import pytest
from mansio_core import Bus, MemoryBackend, SQLiteBackend
from mansio_core.protocols import Backend, ChannelStore
from mansio_core.types import ACLEntry

VERBS = ["read", "publish", "delete", "manage", "write", "admin"]
READER = "reader-agent"
OWNER = "owner-agent"
NOW = "2026-01-01T00:00:00+00:00"

CHANNELS = [
    "unregistered",
    "public-other",
    "private-read",
    "private-publish",
    "private-member",
    "private-none",
    "owned-by-reader",
]


class _NoStoreBackend(Backend):
    """Minimal backend that does not implement ``ChannelStore`` (the NATS shape)."""

    def store(self, message) -> None:
        pass

    def store_queue(self, message) -> None:
        pass

    def get_message(self, message_id: str):
        return None

    def query(self, *args, **kwargs):
        return []

    def list_channels(self):
        return []

    def queue_claim(self, *args, **kwargs):
        return None

    def queue_ack(self, *args, **kwargs):
        return None

    def queue_status(self, message_id: str):
        return None


def _entry(channel: str, permission: str) -> ACLEntry:
    return ACLEntry(
        channel=channel,
        user_id=READER,
        permission=permission,
        granted_at=NOW,
        granted_by=OWNER,
    )


def _seed(bus: Bus) -> None:
    bus.create_channel("public-other", OWNER, visibility="public")
    for name, permission in (
        ("private-read", "read"),
        ("private-publish", "publish"),
        ("private-member", "member"),
    ):
        bus.create_channel(name, OWNER, visibility="private")
        bus.add_acl_entry(_entry(name, permission))
    bus.create_channel("private-none", OWNER, visibility="private")
    bus.create_channel("owned-by-reader", READER, visibility="private")


@pytest.fixture(params=["memory", "sqlite"])
def bus(request, tmp_path):
    backend = (
        MemoryBackend() if request.param == "memory" else SQLiteBackend(str(tmp_path / "bus.db"))
    )
    b = Bus(backend)
    _seed(b)
    yield b
    b.close()


def test_many_matches_single_for_every_verb(bus: Bus) -> None:
    for verb in VERBS:
        many = bus.check_access_many(CHANNELS, READER, verb)
        assert set(many) == set(CHANNELS)
        for name in CHANNELS:
            assert many[name] == bus.check_access(name, READER, verb), (name, verb)


def test_expected_matrix(bus: Bus) -> None:
    read = bus.check_access_many(CHANNELS, READER, "read")
    assert read == {
        "unregistered": True,  # no metadata row is public
        "public-other": True,
        "private-read": True,
        "private-publish": False,  # publish does not imply read
        "private-member": True,
        "private-none": False,
        "owned-by-reader": True,
    }

    publish = bus.check_access_many(CHANNELS, READER, "publish")
    assert publish["public-other"] is True
    assert publish["private-read"] is False
    assert publish["private-publish"] is True
    assert publish["private-member"] is True
    assert publish["owned-by-reader"] is True

    manage = bus.check_access_many(CHANNELS, READER, "manage")
    assert manage["owned-by-reader"] is True
    assert manage["public-other"] is False
    assert manage["private-read"] is False


def test_duplicates_collapsed_and_empty(bus: Bus) -> None:
    assert bus.check_access_many([], READER, "read") == {}
    many = bus.check_access_many(["public-other", "public-other"], READER, "read")
    assert many == {"public-other": True}


def test_no_channel_store_backend_returns_all_true() -> None:
    backend = _NoStoreBackend()
    assert not isinstance(backend, ChannelStore)
    bus = Bus(backend)
    try:
        many = bus.check_access_many(CHANNELS, READER, "read")
        assert many == {name: True for name in CHANNELS}
    finally:
        bus.close()


def test_can_list_channels_matches_can_read(bus: Bus) -> None:
    from mansio_core.authz import ChannelAuthorizer

    authz = ChannelAuthorizer(bus)
    names = [
        *CHANNELS,
        "dm:reader-agent:stranger",  # unregistered DM, reader is a participant
        "dm:someone:else",  # unregistered DM, reader is not
        "notebook:reader-agent",  # reader's own namespace
        "notebook:other",  # someone else's namespace
        "_system:agents",  # unregistered public name
    ]
    many = authz.can_list_channels(READER, names)
    assert set(many) == set(names)
    for name in names:
        # can_read is the pre-batch single-channel rule; the batch path must agree.
        assert many[name] == authz.can_read(READER, name), name

    # Unregistered private names are decided by name, not by the store.
    assert many["dm:reader-agent:stranger"] is True
    assert many["dm:someone:else"] is False
    assert many["notebook:reader-agent"] is True
    assert many["notebook:other"] is False


def test_sqlite_chunks_large_list_in_few_statements(tmp_path) -> None:
    backend = SQLiteBackend(str(tmp_path / "bulk.db"))
    bus = Bus(backend)
    count = 1500
    names = [f"bulk-{i:04d}" for i in range(count)]
    statements: list[str] = []
    connection = backend._conn
    with backend._lock:
        connection.executemany(
            "INSERT INTO channels (name, owner, visibility, created_at) VALUES (?, ?, ?, ?)",
            [(name, OWNER, "private" if i % 2 else "public", NOW) for i, name in enumerate(names)],
        )
        connection.executemany(
            "INSERT INTO channel_acl (channel, user_id, permission, granted_at, granted_by) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (name, READER, "read", NOW, OWNER)
                for name in names
                if int(name.rsplit("-", 1)[1]) % 10 == 1
            ],
        )
        connection.commit()
    connection.set_trace_callback(statements.append)
    try:
        many = bus.check_access_many([*names, "bulk-9999"], READER, "read")
        assert len(many) == count + 1
        # Odd indexes are private; every index ending in 1 also has an ACL row.
        assert many["bulk-0001"] is True  # private + ACL row
        assert many["bulk-0003"] is False  # private, no ACL row
        assert many["bulk-0002"] is True  # public
        assert many["bulk-9999"] is True  # unregistered
        expected_chunks = math.ceil((count + 1) / 500)
        assert len(statements) <= 2 * expected_chunks
    finally:
        connection.set_trace_callback(None)
        bus.close()
