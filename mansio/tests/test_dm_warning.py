"""Tests for DM-to-nonexistent-agent warning.

Verifies that the HTTP publish endpoint returns a warning field when the
target agent has never registered (no presence), and omits it when the
target is known.

Closes #53.
"""

from __future__ import annotations

import pytest

from mansio import Bus, MemoryBackend


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

    def test_message_delivered_despite_warning(self, http_server):
        from mansio._vendor.httpclient import Client as HttpClient

        url, _bus = http_server
        http = HttpClient()
        resp = http.post(
            f"{url}/v1/publish",
            json={
                "channel": "dm:alice:phantom",
                "sender": "alice",
                "msg_type": "text",
                "payload": "test payload",
            },
        )
        assert resp.status_code == 200
        msgs = _bus.query("dm:alice:phantom", limit=10)
        assert len(msgs) == 1
        assert msgs[0].payload == "test payload"
        http.close()
