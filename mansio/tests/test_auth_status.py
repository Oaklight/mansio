"""Tests for /v1/auth/check and auth enforcement consistency."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend


@pytest.fixture()
def closed_server_url():
    """Start a frontend with neither a token store nor the opt-out, yield URL."""
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    yield f"http://{host}:{port}"

    server.shutdown()
    bus.close()


class TestAuthCheckEmptyTokenStore:
    """Server with token store but no tokens: API rejects requests."""

    def test_auth_check_reports_required_no_tokens(self, mansio_server):
        url, _ts, _bus, _srv = mansio_server
        with urllib.request.urlopen(f"{url}/v1/auth/check") as resp:
            data = json.loads(resp.read())
        assert data["auth_mode"] == "required"
        assert data["has_tokens"] is False

    def test_channels_returns_403_when_no_tokens(self, mansio_server):
        url, _ts, _bus, _srv = mansio_server
        req = urllib.request.Request(f"{url}/v1/channels")
        try:
            urllib.request.urlopen(req)
            raise AssertionError("Expected 403")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

    def test_auth_check_after_token_created(self, mansio_server):
        url, token_store, _bus, _srv = mansio_server
        token_store.create_token(user_id="agent-1", label="test")
        with urllib.request.urlopen(f"{url}/v1/auth/check") as resp:
            data = json.loads(resp.read())
        assert data["auth_mode"] == "required"
        assert data["has_tokens"] is True


class TestAuthCheckNoAuth:
    """Server with the explicit unauthenticated opt-out: auth disabled."""

    def test_auth_check_reports_disabled(self, server_url):
        with urllib.request.urlopen(f"{server_url}/v1/auth/check") as resp:
            data = json.loads(resp.read())
        assert data["auth_mode"] == "disabled"
        assert data["has_tokens"] is False

    def test_channels_accessible_without_token(self, server_url):
        with urllib.request.urlopen(f"{server_url}/v1/channels") as resp:
            data = json.loads(resp.read())
        assert "channels" in data


class TestDefaultsClosed:
    """Server constructed with neither a token store nor the opt-out."""

    def test_channels_returns_403(self, closed_server_url):
        req = urllib.request.Request(f"{closed_server_url}/v1/channels")
        try:
            urllib.request.urlopen(req)
            raise AssertionError("Expected 403")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert "no token store" in json.loads(exc.read())["message"]

    def test_publish_returns_403(self, closed_server_url):
        body = json.dumps(
            {
                "channel": "general",
                "sender": "impostor",
                "msg_type": "chat",
                "payload": "hello",
            }
        ).encode()
        req = urllib.request.Request(
            f"{closed_server_url}/v1/publish",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            raise AssertionError("Expected 403")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

    def test_subscribe_returns_403(self, closed_server_url):
        url = f"{closed_server_url}/v1/subscribe?channels=general"
        try:
            urllib.request.urlopen(urllib.request.Request(url), timeout=5)
            raise AssertionError("Expected 403")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

    def test_health_still_public(self, closed_server_url):
        with urllib.request.urlopen(f"{closed_server_url}/health") as resp:
            assert json.loads(resp.read())["status"] == "ok"

    def test_auth_check_reports_required(self, closed_server_url):
        with urllib.request.urlopen(f"{closed_server_url}/v1/auth/check") as resp:
            data = json.loads(resp.read())
        assert data["auth_mode"] == "required"
        assert data["has_tokens"] is False


class TestUnauthenticatedOptOut:
    """An open API is reachable only through the explicit opt-out."""

    def test_publish_accepted_with_opt_out(self, server_url):
        body = json.dumps(
            {
                "channel": "general",
                "sender": "anyone",
                "msg_type": "chat",
                "payload": "hello",
            }
        ).encode()
        req = urllib.request.Request(
            f"{server_url}/v1/publish",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert "message_id" in json.loads(resp.read())

    def test_opt_out_rejects_a_token_store(self, tmp_path):
        from mansio.token_store import TokenStore

        with pytest.raises(ValueError, match="allow_unauthenticated"):
            HttpFrontend(
                port=0,
                token_store=TokenStore(str(tmp_path / "t.db")),
                allow_unauthenticated=True,
            )

    def test_repr_distinguishes_closed_from_opt_out(self):
        assert "auth-unavailable" in repr(HttpFrontend(port=0))
        assert "no-auth" in repr(HttpFrontend(port=0, allow_unauthenticated=True))
