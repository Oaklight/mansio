"""Wire test for SSE subscribe errors (M6d-1).

The parity suite covers channels, ACL, queue, and presence; these pin the
two subscribe-time failures so the move into ``operations.streams`` keeps
their status and body.
"""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_core.authz import DM_NAME_EXPECTED
from mansio_core.token_store import TokenStore
from mansio_web import MansioServer
from mansio_web.adapters import HttpAdapter


def _get(url: str, path: str, token: str | None = None) -> tuple[int, dict]:
    host, port = url.rsplit(":", 1)
    host = host.split("//", 1)[1]
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    data = json.loads(response.read().decode() or "{}")
    status = response.status
    conn.close()
    return status, data


@pytest.fixture()
def subscribe_server(tmp_path):
    store = TokenStore(str(tmp_path / "tokens.db"))
    scoped = store.create_token(user_id="agent-1", label="scoped")["token"]

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
        yield f"http://{host}:{port}", scoped
    finally:
        server.shutdown()
        bus.close()


def test_a_malformed_dm_name_is_400(subscribe_server) -> None:
    url, scoped = subscribe_server
    status, data = _get(url, "/v1/subscribe?channel=dm:onlyone", scoped)
    assert status == 400
    assert data == {
        "error": "Bad Request",
        "message": f"{DM_NAME_EXPECTED} (got 'dm:onlyone')",
    }


def test_an_unreadable_channel_is_403(subscribe_server) -> None:
    url, scoped = subscribe_server
    status, data = _get(url, "/v1/subscribe?channel=secret-room", scoped)
    assert status == 403
    assert data == {
        "error": "Forbidden",
        "message": "Token for 'agent-1' cannot subscribe to: secret-room",
    }
