"""HTTP auth matrix and the admin principal mapping (M6a).

Pins the status code and error body for every way a request can be
authenticated or rejected, because some paths deliberately answer 403 vs
401, and a denied private channel answers 404 to hide its existence.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_core.principal import PrincipalKind
from mansio_core.token_store import TokenStore
from mansio_web import MansioServer
from mansio_web.adapters import HttpAdapter
from mansio_web.adapters.http import _principal_from_validation
from mansio_web.admin import AdminServer
from mansio_web.admin.auth import SessionAuth


def _request(
    url: str,
    path: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: dict | None = None,
) -> tuple[int, dict]:
    headers = {}
    data = None
    if token is not None:
        headers["Authorization"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(url + path, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def auth_server(tmp_path):
    """Auth-required server with a scoped token, a supertoken, and a private channel."""
    store = TokenStore(str(tmp_path / "tokens.db"))
    scoped = store.create_token(user_id="agent-1", label="scoped")["token"]
    supertoken = store.create_token(label="super")["token"]

    bus = Bus(backend=MemoryBackend())
    bus.create_channel("secret-room", "other-agent", visibility="private")
    adapter = HttpAdapter(host="127.0.0.1", port=0, token_store=store)
    server = MansioServer(bus)
    server.add_adapter(adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    host, port = adapter.address
    try:
        yield f"http://{host}:{port}", scoped, supertoken
    finally:
        server.shutdown()
        bus.close()


@pytest.fixture()
def no_auth_server():
    bus = Bus(backend=MemoryBackend())
    adapter = HttpAdapter(host="127.0.0.1", port=0, allow_unauthenticated=True)
    server = MansioServer(bus)
    server.add_adapter(adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    host, port = adapter.address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        bus.close()


class TestAuthMatrix:
    def test_scoped_token_is_accepted(self, auth_server) -> None:
        url, scoped, _ = auth_server
        status, body = _request(url, "/v1/channels", token=f"Bearer {scoped}")
        assert status == 200
        assert "channels" in body

    def test_supertoken_is_accepted(self, auth_server) -> None:
        url, _, supertoken = auth_server
        status, body = _request(url, "/v1/channels", token=f"Bearer {supertoken}")
        assert status == 200
        assert "channels" in body

    def test_no_auth_is_accepted(self, no_auth_server) -> None:
        status, body = _request(no_auth_server, "/v1/channels")
        assert status == 200
        assert "channels" in body

    def test_missing_header_is_401(self, auth_server) -> None:
        url, _, _ = auth_server
        status, body = _request(url, "/v1/channels")
        assert status == 401
        assert body == {"error": "Unauthorized", "message": "Missing Authorization header"}

    def test_malformed_header_is_401(self, auth_server) -> None:
        url, _, _ = auth_server
        status, body = _request(url, "/v1/channels", token="Token abc")
        assert status == 401
        assert body == {"error": "Unauthorized", "message": "Expected 'Bearer <token>'"}

    def test_invalid_token_is_401(self, auth_server) -> None:
        url, _, _ = auth_server
        status, body = _request(url, "/v1/channels", token="Bearer mst-not-a-token")
        assert status == 401
        assert body == {"error": "Unauthorized", "message": "Invalid or expired token"}

    def test_invalid_token_on_queue_claim_is_401_before_the_handler(self, auth_server) -> None:
        """TokenStore.validate() == False never becomes a principal."""
        url, _, _ = auth_server
        status, body = _request(
            url,
            "/v1/queue/claim",
            token="Bearer mst-not-a-token",
            method="POST",
            body={"channel": "agents"},
        )
        assert status == 401
        assert body == {"error": "Unauthorized", "message": "Invalid or expired token"}

    def test_token_bound_sender_is_403(self, auth_server) -> None:
        url, scoped, _ = auth_server
        status, body = _request(
            url,
            "/v1/publish",
            token=f"Bearer {scoped}",
            method="POST",
            body={
                "channel": "agents",
                "sender": "other-agent",
                "payload": "hi",
                "msg_type": "chat",
            },
        )
        assert status == 403
        assert body == {
            "error": "Forbidden",
            "message": "Token bound to 'agent-1', cannot publish as 'other-agent'",
        }

    def test_private_channel_meta_is_404_for_an_outsider(self, auth_server) -> None:
        url, scoped, _ = auth_server
        status, body = _request(url, "/v1/channels/secret-room/meta", token=f"Bearer {scoped}")
        assert status == 404
        assert body == {"error": "Not Found", "message": "Channel 'secret-room' not found"}

    def test_no_tokens_configured_is_403(self, tmp_path) -> None:
        store = TokenStore(str(tmp_path / "empty-tokens.db"))
        bus = Bus(backend=MemoryBackend())
        adapter = HttpAdapter(host="127.0.0.1", port=0, token_store=store)
        server = MansioServer(bus)
        server.add_adapter(adapter)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.3)
        host, port = adapter.address
        try:
            status, body = _request(f"http://{host}:{port}", "/v1/channels")
        finally:
            server.shutdown()
            bus.close()
        assert status == 403
        assert body == {
            "error": "Forbidden",
            "message": (
                "No API tokens configured. Create a token via the admin panel before connecting."
            ),
        }


class TestValidationMapping:
    def test_scoped_id_maps_to_user(self) -> None:
        principal = _principal_from_validation("agent-1")
        assert principal.kind is PrincipalKind.USER
        assert principal.user_id == "agent-1"

    def test_none_maps_to_supertoken(self) -> None:
        principal = _principal_from_validation(None)
        assert principal.kind is PrincipalKind.SUPERTOKEN

    @pytest.mark.parametrize("value", [True, False, object()])
    def test_unexpected_result_never_becomes_privileged(self, value: object) -> None:
        with pytest.raises(TypeError, match="unexpected token validation result"):
            _principal_from_validation(value)  # type: ignore[arg-type]


class TestPublicPaths:
    def test_public_paths_get_no_principal(self, tmp_path) -> None:
        """The middleware lets /health and /v1/auth/check through untouched."""
        store = TokenStore(str(tmp_path / "tokens.db"))
        store.create_token(user_id="agent-1", label="scoped")
        bus = Bus(backend=MemoryBackend())
        adapter = HttpAdapter(host="127.0.0.1", port=0, token_store=store)
        adapter.attach(bus)
        try:
            middleware = adapter._app._before_request_handlers[0]
            for path in ("/health", "/v1/auth/check"):
                request = SimpleNamespace(
                    method="GET", path=path, headers={}, state=SimpleNamespace()
                )
                assert asyncio.run(middleware(request)) is None
                assert not hasattr(request.state, "principal"), path
        finally:
            bus.close()


def _admin_middleware(admin: AdminServer):
    return admin._app._before_request_handlers[0]


def _admin_request(path: str, session: str | None = None):
    cookies = {SessionAuth.COOKIE_NAME: session} if session else {}
    return SimpleNamespace(
        method="GET", path=path, cookies=cookies, headers={}, state=SimpleNamespace()
    )


class TestAdminPrincipal:
    def test_sessionless_loopback_gets_no_auth(self) -> None:
        bus = Bus(backend=MemoryBackend())
        admin = AdminServer(bus, port=0)
        try:
            request = _admin_request("/api/stats")
            assert _admin_middleware(admin)(request) is None
            assert request.state.principal.kind is PrincipalKind.NO_AUTH
            assert request.state.principal.user_id is None
        finally:
            admin.stop()
            bus.close()

    def test_sessionless_loopback_request_still_succeeds(self) -> None:
        bus = Bus(backend=MemoryBackend())
        admin = AdminServer(bus, port=0)
        info = admin.start()
        try:
            status, body = _request(info.url, "/api/stats")
            assert status == 200
            assert "total_messages" in body
        finally:
            admin.stop()
            bus.close()

    def test_valid_session_gets_admin(self) -> None:
        bus = Bus(backend=MemoryBackend())
        admin = AdminServer(bus, port=0, auth_password="secret123")
        try:
            assert admin._auth is not None
            request = _admin_request("/api/stats", session=admin._auth.create_session())
            assert _admin_middleware(admin)(request) is None
            assert request.state.principal.kind is PrincipalKind.ADMIN
        finally:
            admin.stop()
            bus.close()

    def test_session_server_rejects_without_a_cookie(self) -> None:
        bus = Bus(backend=MemoryBackend())
        admin = AdminServer(bus, port=0, auth_password="secret123")
        info = admin.start()
        try:
            status, body = _request(info.url, "/api/stats")
            assert status == 401
            assert body == {
                "error": "Unauthorized",
                "message": "Admin authentication required",
            }
            request = _admin_request("/api/stats")
            _admin_middleware(admin)(request)
            assert not hasattr(request.state, "principal")
        finally:
            admin.stop()
            bus.close()
