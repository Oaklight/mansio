"""Tests for /api/users admin endpoints."""

from __future__ import annotations

import pytest

from mansio import Bus, MemoryBackend
from mansio._vendor.httpclient import Client as HttpClient
from mansio.admin.server import AdminServer
from mansio.token_store import TokenStore


@pytest.fixture()
def admin_api(tmp_path):
    """Start AdminServer, yield (url, token_store, bus)."""
    db_path = str(tmp_path / "admin-test.db")
    token_store = TokenStore(db_path)
    bus = Bus(backend=MemoryBackend())
    admin = AdminServer(bus, port=0, token_store=token_store)
    info = admin.start()
    yield info.url, token_store, bus
    admin.stop()
    bus.close()


def _post(http, url, path, json=None):
    return http.post(f"{url}{path}", json=json or {})


def _get(http, url, path):
    return http.get(f"{url}{path}")


def _delete(http, url, path):
    return http.delete(f"{url}{path}")


class TestRegisterUser:
    def test_register_returns_token(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(http, url, "/api/users", {"user_id": "alice"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert body["user_id"] == "alice"
        assert "token" in body["token"]
        http.close()

    def test_register_conflict(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        _post(http, url, "/api/users", {"user_id": "alice"})
        resp = _post(http, url, "/api/users", {"user_id": "alice"})
        assert resp.status_code == 409
        http.close()

    def test_register_empty_user_id(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(http, url, "/api/users", {"user_id": ""})
        assert resp.status_code == 400
        http.close()

    def test_register_invalid_format(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(http, url, "/api/users", {"user_id": "UPPERCASE"})
        assert resp.status_code == 400
        assert "lowercase" in resp.json()["message"]
        http.close()

    def test_register_too_short(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(http, url, "/api/users", {"user_id": "ab"})
        assert resp.status_code == 400
        http.close()


class TestListUsers:
    def test_empty(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _get(http, url, "/api/users")
        assert resp.status_code == 200
        assert resp.json()["users"] == []
        http.close()

    def test_lists_registered_users(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        _post(http, url, "/api/users", {"user_id": "alice"})
        _post(http, url, "/api/users", {"user_id": "bob-agent"})
        resp = _get(http, url, "/api/users")
        users = resp.json()["users"]
        ids = [u["user_id"] for u in users]
        assert "alice" in ids
        assert "bob-agent" in ids
        http.close()


class TestGetUser:
    def test_found(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        _post(http, url, "/api/users", {"user_id": "alice"})
        resp = _get(http, url, "/api/users/alice")
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "alice"
        assert len(body["tokens"]) == 1
        http.close()

    def test_not_found(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _get(http, url, "/api/users/ghost")
        assert resp.status_code == 404
        http.close()


class TestDeleteUser:
    def test_delete_removes_all_tokens(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        _post(http, url, "/api/users", {"user_id": "alice"})
        _post(http, url, "/api/users/alice/tokens", {"label": "second"})
        resp = _delete(http, url, "/api/users/alice")
        assert resp.status_code == 200
        assert resp.json()["deleted_tokens"] == 2
        assert _get(http, url, "/api/users/alice").status_code == 404
        http.close()

    def test_delete_not_found(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _delete(http, url, "/api/users/ghost")
        assert resp.status_code == 404
        http.close()


class TestUserTokens:
    def test_list_tokens(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        _post(http, url, "/api/users", {"user_id": "alice"})
        resp = _get(http, url, "/api/users/alice/tokens")
        assert resp.status_code == 200
        assert len(resp.json()["tokens"]) == 1
        http.close()

    def test_create_additional_token(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        _post(http, url, "/api/users", {"user_id": "alice"})
        resp = _post(http, url, "/api/users/alice/tokens", {"label": "second"})
        assert resp.status_code == 201
        assert _get(http, url, "/api/users/alice/tokens").json()["tokens"].__len__() == 2
        http.close()

    def test_create_token_user_not_found(self, admin_api):
        url, store, bus = admin_api
        http = HttpClient()
        resp = _post(http, url, "/api/users/ghost/tokens", {"label": "orphan"})
        assert resp.status_code == 404
        http.close()
