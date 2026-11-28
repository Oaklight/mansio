"""Duplicate channel creation must not wedge authenticated requests (issue #425).

The default configuration points the message backend and the token store at
the same SQLite file.  When a duplicate ``POST /v1/channels`` left the bus
connection's transaction open, the next authenticated request blocked in
``UPDATE tokens SET last_used_at`` and never returned.  The client here uses a
2 s timeout, so the regression fails fast instead of hanging the suite.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse

import pytest
from mansio_core import Bus, SQLiteBackend
from mansio_core.token_store import TokenStore
from mansio_web import MansioServer
from mansio_web.adapters import HttpAdapter

_CLIENT_TIMEOUT = 2.0


def _req(url: str, method: str = "GET", token: str | None = None, body: dict | None = None):
    parsed = urllib.parse.urlparse(url)
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=_CLIENT_TIMEOUT)
    raw = json.dumps(body).encode() if body is not None else None
    headers: dict[str, str] = {}
    if raw is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        conn.request(method, target, body=raw, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode() or "{}")
        return resp.status, payload
    finally:
        conn.close()


@pytest.fixture()
def shared_db_server(tmp_path):
    """Server with one SQLite file behind both the backend and the tokens."""
    db_path = str(tmp_path / "shared.db")
    backend = SQLiteBackend(db_path)
    token_store = TokenStore(db_path)
    bus = Bus(backend=backend)
    adapter = HttpAdapter(host="127.0.0.1", port=0, token_store=token_store)
    server = MansioServer(bus)
    server.add_adapter(adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    host, port = adapter.address
    try:
        yield f"http://{host}:{port}", token_store
    finally:
        server.shutdown()
        bus.close()
        backend.close()


def test_duplicate_create_does_not_block_authenticated_requests(shared_db_server) -> None:
    url, store = shared_db_server
    token = store.create_token(user_id="alice", label="alice test token")["token"]

    status, first = _req(f"{url}/v1/channels", "POST", token, {"name": "dup"})
    assert status == 201, first

    status, conflict = _req(f"{url}/v1/channels", "POST", token, {"name": "dup"})
    assert status == 409, conflict
    assert conflict["error"] == "Conflict"
    assert "already exists" in conflict["message"]

    # The next authenticated request touches the token row on the same file.
    status, channels = _req(f"{url}/v1/channels", "GET", token)
    assert status == 200, channels
    assert isinstance(channels["channels"], list)
