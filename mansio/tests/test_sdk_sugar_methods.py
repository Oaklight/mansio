"""Regression tests for SDK sugar methods — fix for #96.

Verifies that MansioClient.note_write, thought_record, and memory_store
work correctly through the HTTP transport without crashing the server.

The original bug (v0.2.3) caused a server crash when these methods were
called via the HTTP transport. The async httpserver refactor fixed the
root cause; these tests prevent regression.
"""

from __future__ import annotations

import threading
import time

from mansio_client import MansioClient

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend


def _make_server(tmp_path):
    """Start a MansioServer with token auth, return (url, token_store, server)."""
    from mansio.token_store import TokenStore

    db_path = str(tmp_path / "sdk_sugar_test.db")
    token_store = TokenStore(db_path)

    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0, token_store=token_store)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    url = f"http://{host}:{port}"
    return url, token_store, server


class TestNoteWrite:
    """note_write must publish to notebook:{agent_id} without crashing."""

    def test_note_write_returns_message_id(self, tmp_path) -> None:
        url, store, server = _make_server(tmp_path)
        try:
            token = store.create_token("elena", "Elena")["token"]
            client = MansioClient(url, "elena", token=token)

            msg_id = client.note_write("test note content")
            assert msg_id, "note_write should return a message ID"
            assert isinstance(msg_id, str)

            client.close()
        finally:
            server.shutdown()

    def test_note_write_with_tags(self, tmp_path) -> None:
        url, store, server = _make_server(tmp_path)
        try:
            token = store.create_token("elena", "Elena")["token"]
            client = MansioClient(url, "elena", token=token)

            msg_id = client.note_write("tagged note", tags=["test", "important"])
            assert msg_id

            notes = client.note_read()
            assert len(notes) == 1
            assert notes[0].payload == "tagged note"
            assert notes[0].msg_type == "note"
            assert notes[0].metadata is not None
            assert notes[0].metadata.get("tags") == ["test", "important"]

            client.close()
        finally:
            server.shutdown()

    def test_note_read_roundtrip(self, tmp_path) -> None:
        url, store, server = _make_server(tmp_path)
        try:
            token = store.create_token("elena", "Elena")["token"]
            client = MansioClient(url, "elena", token=token)

            client.note_write("note one")
            client.note_write("note two")

            notes = client.note_read(limit=10)
            assert len(notes) == 2
            payloads = [n.payload for n in notes]
            assert "note one" in payloads
            assert "note two" in payloads

            client.close()
        finally:
            server.shutdown()


class TestThoughtRecord:
    """thought_record must publish to notebook:{agent_id} without crashing."""

    def test_thought_record_returns_message_id(self, tmp_path) -> None:
        url, store, server = _make_server(tmp_path)
        try:
            token = store.create_token("elena", "Elena")["token"]
            client = MansioClient(url, "elena", token=token)

            msg_id = client.thought_record("reasoning", "test focus", "thinking about stuff")
            assert msg_id
            assert isinstance(msg_id, str)

            client.close()
        finally:
            server.shutdown()

    def test_thought_read_roundtrip(self, tmp_path) -> None:
        url, store, server = _make_server(tmp_path)
        try:
            token = store.create_token("elena", "Elena")["token"]
            client = MansioClient(url, "elena", token=token)

            client.thought_record("planning", "architecture", "designing the system")
            client.thought_record("reflection", "code review", "reviewing the PR")

            thoughts = client.thought_read(limit=10)
            assert len(thoughts) == 2
            assert all(t.msg_type == "thought" for t in thoughts)

            client.close()
        finally:
            server.shutdown()


class TestMemoryStore:
    """memory_store must publish to memory:{agent_id} without crashing."""

    def test_memory_store_returns_message_id(self, tmp_path) -> None:
        url, store, server = _make_server(tmp_path)
        try:
            token = store.create_token("elena", "Elena")["token"]
            client = MansioClient(url, "elena", token=token)

            msg_id = client.memory_store("important fact")
            assert msg_id
            assert isinstance(msg_id, str)

            client.close()
        finally:
            server.shutdown()

    def test_memory_store_with_type(self, tmp_path) -> None:
        url, store, server = _make_server(tmp_path)
        try:
            token = store.create_token("elena", "Elena")["token"]
            client = MansioClient(url, "elena", token=token)

            client.memory_store("user prefers dark mode", memory_type="preference")

            memories = client.memory_recall("dark mode")
            assert len(memories) == 1
            assert memories[0].payload == "user prefers dark mode"
            assert memories[0].msg_type == "memory"

            client.close()
        finally:
            server.shutdown()

    def test_memory_recall_roundtrip(self, tmp_path) -> None:
        url, store, server = _make_server(tmp_path)
        try:
            token = store.create_token("elena", "Elena")["token"]
            client = MansioClient(url, "elena", token=token)

            client.memory_store("python is great")
            client.memory_store("rust is fast")
            client.memory_store("go is simple")

            python_memories = client.memory_recall("python")
            assert len(python_memories) == 1
            assert "python" in python_memories[0].payload.lower()

            rust_memories = client.memory_recall("rust")
            assert len(rust_memories) == 1
            assert "rust" in rust_memories[0].payload.lower()

            client.close()
        finally:
            server.shutdown()


class TestServerStability:
    """Server must not crash when SDK sugar methods are called in sequence."""

    def test_all_sugar_methods_in_sequence(self, tmp_path) -> None:
        """Call all sugar methods back-to-back — server must stay up."""
        url, store, server = _make_server(tmp_path)
        try:
            token = store.create_token("elena", "Elena")["token"]
            client = MansioClient(url, "elena", token=token)

            # All three sugar methods in sequence
            client.note_write("a note")
            client.thought_record("reasoning", "test", "a thought")
            client.memory_store("a memory")

            # Server should still be healthy — verify with channel_list
            channels = client.channel_list()
            assert "notebook:elena" in channels
            assert "memory:elena" in channels

            # Verify reads work too
            assert len(client.note_read()) == 1
            assert len(client.thought_read()) == 1
            assert len(client.memory_recall("memory")) == 1

            client.close()
        finally:
            server.shutdown()

    def test_multiple_agents_sugar_methods(self, tmp_path) -> None:
        """Multiple agents using sugar methods concurrently."""
        url, store, server = _make_server(tmp_path)
        try:
            t1 = store.create_token("alice", "Alice")["token"]
            t2 = store.create_token("bob", "Bob")["token"]

            alice = MansioClient(url, "alice", token=t1)
            bob = MansioClient(url, "bob", token=t2)

            alice.note_write("alice note")
            bob.note_write("bob note")
            alice.memory_store("alice memory")
            bob.thought_record("planning", "test", "bob thought")

            # Each sees only their own data
            assert len(alice.note_read()) == 1
            assert alice.note_read()[0].payload == "alice note"
            assert len(bob.note_read()) == 1
            assert bob.note_read()[0].payload == "bob note"

            alice.close()
            bob.close()
        finally:
            server.shutdown()
