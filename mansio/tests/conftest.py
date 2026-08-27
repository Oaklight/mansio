"""Shared fixtures for mansio integration tests."""

from __future__ import annotations

import threading
import time

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend
from mansio.token_store import TokenStore


@pytest.fixture()
def mansio_server(tmp_path):
    """Start a mansio server with HTTP frontend, yield (url, token_store, bus, server)."""
    db_path = str(tmp_path / "test-tokens.db")
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

    yield url, token_store, bus, server

    server.shutdown()
    bus.close()


def make_client(url: str, token_store: TokenStore, agent_id: str = "test-agent"):
    """Create a mansio_client.MansioClient with a fresh token."""
    from mansio_client import MansioClient

    entry = token_store.create_token(agent_id=agent_id, label=f"{agent_id} test token")
    return MansioClient(url, agent_id, token=entry["token"])


@pytest.fixture()
def server_url(tmp_path):
    """Start a mansio server without token auth, yield URL."""
    import threading
    import time

    from mansio.frontends import HttpFrontend

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
