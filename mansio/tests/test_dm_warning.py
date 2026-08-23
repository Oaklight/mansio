"""Tests for DM-to-nonexistent-agent warning.

Verifies that dm_send() warns when the target agent has never registered,
and stays silent when the target is known.

Closes #53.
"""

from __future__ import annotations

import warnings

import pytest

from mansio import Bus, MansioClient, MemoryBackend


@pytest.fixture()
def bus():
    b = Bus(backend=MemoryBackend())
    yield b
    b.close()


def _make_client(bus: Bus, agent_id: str) -> MansioClient:
    return MansioClient(bus, agent_id)


class TestDMWarningLocal:
    """Client-side UserWarning via local transport."""

    def test_warns_on_nonexistent_target(self, bus):
        sender = _make_client(bus, "alice")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            msg_id = sender.dm_send("ghost-agent", "hello?")
            assert msg_id  # message still stored
            assert len(w) == 1
            assert issubclass(w[0].category, UserWarning)
            assert "ghost-agent" in str(w[0].message)
            assert "not registered" in str(w[0].message)

    def test_no_warning_when_target_registered(self, bus):
        sender = _make_client(bus, "alice")
        # Register the target via heartbeat.
        bus.heartbeat("bob")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            msg_id = sender.dm_send("bob", "hey bob")
            assert msg_id
            assert len(w) == 0

    def test_message_delivered_despite_warning(self, bus):
        sender = _make_client(bus, "alice")
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            sender.dm_send("phantom", "test payload")

        # The message should exist in the DM channel.
        msgs = bus.query("dm:alice:phantom", limit=10)
        assert len(msgs) == 1
        assert msgs[0].payload == "test payload"

    def test_no_warning_self_dm(self, bus):
        """Sending a DM to yourself shouldn't warn (you exist)."""
        sender = _make_client(bus, "alice")
        bus.heartbeat("alice")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            sender.dm_send("alice", "note to self")
            assert len(w) == 0


class TestDMWarningHTTP:
    """HTTP endpoint warning field for DM to nonexistent agent."""

    @pytest.fixture()
    def http_server(self):
        """Start a server and yield (url, bus) so tests can register agents."""
        import threading
        import time

        from mansio import MansioServer
        from mansio.frontends import HttpFrontend

        _bus = Bus(backend=MemoryBackend())
        frontend = HttpFrontend(host="127.0.0.1", port=0)
        server = MansioServer(_bus)
        server.add_frontend(frontend)

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.3)

        host, port = frontend.address
        yield f"http://{host}:{port}", _bus

        server.shutdown()

    def test_http_warning_on_nonexistent_target(self, http_server):
        """POST /v1/publish to a DM channel should include a 'warning' field
        when the target agent has no presence."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, _ = http_server
        http = HttpClient()
        resp = http.post(
            f"{url}/v1/publish",
            json={
                "channel": "dm:alice:ghost",
                "sender": "alice",
                "msg_type": "text",
                "payload": "hello ghost",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "message_id" in body
        assert "warning" in body
        assert "ghost" in body["warning"]
        assert "not registered" in body["warning"]
        http.close()

    def test_http_no_warning_when_target_registered(self, http_server):
        """No warning field when the target agent has registered."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, _bus = http_server
        _bus.heartbeat("bob")

        http = HttpClient()
        resp = http.post(
            f"{url}/v1/publish",
            json={
                "channel": "dm:alice:bob",
                "sender": "alice",
                "msg_type": "text",
                "payload": "hey bob",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "message_id" in body
        assert "warning" not in body
        http.close()
