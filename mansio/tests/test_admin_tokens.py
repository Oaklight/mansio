"""Tests for ``/api/tokens`` and explicit supertoken creation (issue #299)."""

from __future__ import annotations

import threading
import time

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio._vendor.httpclient import Client as HttpClient
from mansio.admin.server import AdminServer
from mansio.frontends import HttpFrontend
from mansio.token_store import TokenStore


@pytest.fixture()
def admin_api(tmp_path):
    """Start AdminServer, yield (url, token_store, bus)."""
    db_path = str(tmp_path / "admin-tokens.db")
    token_store = TokenStore(db_path)
    bus = Bus(backend=MemoryBackend())
    admin = AdminServer(bus, port=0, token_store=token_store)
    info = admin.start()
    yield info.url, token_store, bus
    admin.stop()
    bus.close()


def _post(http, url, path, json=None):
    return http.post(f"{url}{path}", json=json or {})


class TestCreateSupertoken:
    """Explicit ``{"supertoken": true}`` creation path."""

    def test_create_supertoken(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(http, url, "/api/tokens", {"supertoken": True, "label": "ops"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert body["supertoken"] is True
        assert body["token"]["user_id"] is None
        assert body["token"]["label"] == "ops"
        assert store.validate(body["token"]["token"]) is None
        http.close()

    def test_supertoken_appears_in_token_list(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        _post(http, url, "/api/tokens", {"supertoken": True, "label": "ops"})
        resp = http.get(f"{url}/api/tokens")
        entries = resp.json()["tokens"]
        assert [e["user_id"] for e in entries] == [None]
        http.close()

    def test_reject_user_id_with_supertoken(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(
            http,
            url,
            "/api/tokens",
            {"supertoken": True, "user_id": "alice"},
        )
        assert resp.status_code == 400
        assert "omitted" in resp.json()["message"]
        assert store.list_tokens() == []
        http.close()

    def test_reject_non_boolean_supertoken(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(http, url, "/api/tokens", {"supertoken": "yes", "user_id": "alice"})
        assert resp.status_code == 400
        assert "boolean" in resp.json()["message"]
        assert store.list_tokens() == []
        http.close()


class TestCreateUserToken:
    """The existing user-bound path keeps its contract."""

    def test_create_user_token(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(http, url, "/api/tokens", {"user_id": "alice", "label": "agent"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert "supertoken" not in body
        assert body["token"]["user_id"] == "alice"
        assert store.validate(body["token"]["token"]) == "alice"
        http.close()

    @pytest.mark.parametrize("payload", [{}, {"user_id": ""}, {"user_id": "   "}])
    def test_empty_user_id_still_rejected(self, admin_api, payload):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(http, url, "/api/tokens", payload)
        assert resp.status_code == 400
        assert store.list_tokens() == []
        http.close()


class TestSupertokenEndToEnd:
    """A supertoken minted by the admin API works on the HTTP frontend."""

    def test_supertoken_can_publish_to_broadcast(self, tmp_path):
        bus = Bus(backend=MemoryBackend())
        token_store = TokenStore(str(tmp_path / "e2e.db"))

        http_frontend = HttpFrontend(host="127.0.0.1", port=0, token_store=token_store)
        server = MansioServer(bus)
        server.add_frontend(http_frontend)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        admin = AdminServer(port=0, token_store=token_store)
        admin.attach(bus)
        info = admin.start()

        http = HttpClient()
        try:
            time.sleep(0.3)
            created = _post(http, info.url, "/api/tokens", {"supertoken": True})
            assert created.status_code == 201
            token = created.json()["token"]["token"]

            host, port = http_frontend.address
            resp = http.post(
                f"http://{host}:{port}/v1/publish",
                json={
                    "channel": "broadcast:announcements",
                    "sender": "release-bot",
                    "msg_type": "announcement",
                    "payload": "v1.2.3 shipped",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, resp.json()
            assert resp.json()["message_id"]
        finally:
            http.close()
            admin.stop()
            server.shutdown()
            bus.close()
