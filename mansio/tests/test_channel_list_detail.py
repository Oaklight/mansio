"""Tests for channel_list detail metadata (issue #56)."""

from __future__ import annotations

import threading
import time

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.backends.memory import MemoryBackend as MemoryBackendDirect
from mansio.backends.memory import _infer_channel_type as mem_infer
from mansio.backends.sqlite import SQLiteBackend
from mansio.backends.sqlite import _infer_channel_type as sql_infer
from mansio.frontends import HttpFrontend

# ── Channel type inference ────────────────────────────────────────


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        ("general", "user"),
        ("my-channel", "user"),
        ("dm:alice:bob", "dm"),
        ("notebook:agent-a", "notebook"),
        ("memory:agent-b", "memory"),
        ("broadcast:news", "broadcast"),
        ("_system:registry", "system"),
        ("_system:cursors:agent-a", "system"),
    ],
)
def test_infer_channel_type_sqlite(channel: str, expected: str) -> None:
    assert sql_infer(channel) == expected


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        ("general", "user"),
        ("dm:alice:bob", "dm"),
        ("notebook:agent-a", "notebook"),
        ("memory:agent-b", "memory"),
        ("broadcast:news", "broadcast"),
        ("_system:registry", "system"),
    ],
)
def test_infer_channel_type_memory(channel: str, expected: str) -> None:
    assert mem_infer(channel) == expected


# ── SQLite backend ────────────────────────────────────────────────


class TestSQLiteListChannelsDetail:
    def test_empty_db(self) -> None:
        backend = SQLiteBackend(":memory:")
        assert backend.list_channels_detail() == []
        backend.close()

    def test_metadata_values(self) -> None:
        backend = SQLiteBackend(":memory:")
        bus = Bus(backend=backend)

        bus.publish("general", "alice", "chat", "hello")
        bus.publish("general", "bob", "chat", "hi")
        bus.publish("general", "alice", "chat", "howdy")
        bus.publish("dm:alice:bob", "alice", "chat", "secret")

        detail = backend.list_channels_detail()
        assert len(detail) == 2

        # Channels are sorted by name
        dm_ch = detail[0]
        gen_ch = detail[1]

        assert dm_ch["name"] == "dm:alice:bob"
        assert dm_ch["message_count"] == 1
        assert dm_ch["sender_count"] == 1
        assert dm_ch["type"] == "dm"
        assert dm_ch["last_activity"]  # non-empty timestamp

        assert gen_ch["name"] == "general"
        assert gen_ch["message_count"] == 3
        assert gen_ch["sender_count"] == 2
        assert gen_ch["type"] == "user"
        assert gen_ch["last_activity"]
        backend.close()

    def test_expected_keys(self) -> None:
        backend = SQLiteBackend(":memory:")
        bus = Bus(backend=backend)
        bus.publish("test-chan", "agent", "chat", "msg")

        detail = backend.list_channels_detail()
        assert len(detail) == 1
        entry = detail[0]
        assert set(entry.keys()) == {
            "name",
            "message_count",
            "last_activity",
            "sender_count",
            "type",
        }
        backend.close()


# ── Memory backend ───────────────────────────────────────────────


class TestMemoryListChannelsDetail:
    def test_empty(self) -> None:
        backend = MemoryBackendDirect()
        assert backend.list_channels_detail() == []

    def test_metadata_values(self) -> None:
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)

        bus.publish("general", "alice", "chat", "hello")
        bus.publish("general", "bob", "chat", "hi")
        bus.publish("general", "alice", "chat", "howdy")
        bus.publish("notebook:alice", "alice", "note", "my note")

        detail = backend.list_channels_detail()
        assert len(detail) == 2

        gen_ch = next(d for d in detail if d["name"] == "general")
        nb_ch = next(d for d in detail if d["name"] == "notebook:alice")

        assert gen_ch["message_count"] == 3
        assert gen_ch["sender_count"] == 2
        assert gen_ch["type"] == "user"
        assert gen_ch["last_activity"]

        assert nb_ch["message_count"] == 1
        assert nb_ch["sender_count"] == 1
        assert nb_ch["type"] == "notebook"

    def test_expected_keys(self) -> None:
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        bus.publish("test-chan", "agent", "chat", "msg")

        detail = backend.list_channels_detail()
        assert len(detail) == 1
        entry = detail[0]
        assert set(entry.keys()) == {
            "name",
            "message_count",
            "last_activity",
            "sender_count",
            "type",
        }


# ── Bus.channels_detail() ────────────────────────────────────────


class TestBusChannelsDetail:
    def test_delegates_to_backend(self) -> None:
        bus = Bus(backend=MemoryBackend())
        bus.publish("general", "alice", "chat", "hello")
        detail = bus.channels_detail()
        assert len(detail) == 1
        assert detail[0]["name"] == "general"


# ── HTTP endpoint ─────────────────────────────────────────────────


@pytest.fixture()
def detail_server_url():
    """Start a MansioServer with HttpFrontend, yield URL."""
    bus = Bus(backend=MemoryBackend())

    # Seed some data
    bus.publish("general", "alice", "chat", "hello")
    bus.publish("general", "bob", "chat", "hi")
    bus.publish("dm:alice:bob", "alice", "chat", "secret")

    frontend = HttpFrontend(host="127.0.0.1", port=0)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    url = f"http://{host}:{port}"

    yield url

    server.shutdown()


class TestHTTPChannelListDetail:
    def test_backward_compat_no_detail(self, detail_server_url: str) -> None:
        """Without detail param, returns list of strings."""
        from mansio import MansioClient

        with MansioClient(detail_server_url, "test-agent") as client:
            channels = client.channel_list()
            assert isinstance(channels, list)
            # All items should be strings
            assert all(isinstance(ch, str) for ch in channels)

    def test_detail_true_returns_dicts(self, detail_server_url: str) -> None:
        """With detail=True, returns list of dicts with metadata."""
        from mansio import MansioClient

        with MansioClient(detail_server_url, "test-agent") as client:
            channels: list[dict] = client.channel_list(detail=True)  # type: ignore[assignment]
            assert isinstance(channels, list)
            assert len(channels) >= 2  # general + dm:alice:bob

            # Find the general channel
            general = [ch for ch in channels if ch["name"] == "general"]
            assert len(general) == 1
            g = general[0]
            assert g["message_count"] == 2
            assert g["sender_count"] == 2
            assert g["type"] == "user"
            assert "last_activity" in g

    def test_detail_false_returns_strings(self, detail_server_url: str) -> None:
        """Explicit detail=False returns strings."""
        from mansio import MansioClient

        with MansioClient(detail_server_url, "test-agent") as client:
            channels = client.channel_list(detail=False)
            assert isinstance(channels, list)
            assert all(isinstance(ch, str) for ch in channels)
