"""Token auth must work the same way regardless of the message backend.

TokenStore opens its own SQLite file and never touches the message
backend, so nothing about it depends on which Backend a server uses.
These tests wire an HttpFrontend to the Maildir backend (no external
service required) and, when a NATS server is reachable, to the NATS
backend, and drive each one through the same auth-enforcement checks
already exercised against MemoryBackend elsewhere in the suite.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
from conftest import make_client

from mansio import Bus, MansioServer
from mansio.backends.maildir import MaildirBackend
from mansio.frontends import HttpFrontend
from mansio.token_store import TokenStore


def _start_server(bus: Bus, token_store: TokenStore):
    """Start an HttpFrontend over *bus*, yield (url, server) once bound."""
    frontend = HttpFrontend(host="127.0.0.1", port=0, token_store=token_store)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    return f"http://{host}:{port}", server


@pytest.fixture()
def maildir_auth_server(tmp_path):
    """Maildir-backed server with a token store in a separate file.

    The token store's file lives next to, but is otherwise unrelated to,
    the maildir message directory.
    """
    bus = Bus(backend=MaildirBackend(str(tmp_path / "maildir")))
    token_store = TokenStore(str(tmp_path / "tokens.db"))

    url, server = _start_server(bus, token_store)
    yield url, token_store

    server.shutdown()
    bus.close()


def _run_publish_round_trip(server_url_and_store) -> None:
    """Publish and read back a message using a freshly issued token."""
    url, token_store = server_url_and_store

    client = make_client(url, token_store, user_id="agent-a")
    client.channel_send("general", "hello from a token-authenticated client")
    messages = client.channel_read("general")
    assert any(m.payload == "hello from a token-authenticated client" for m in messages)
    client.close()


class TestMaildirBackendHasWorkingAuth:
    """A Maildir-backed server enforces and honors token auth, per issue #285."""

    def test_rejects_requests_without_a_token(self, maildir_auth_server):
        url, _token_store = maildir_auth_server
        req = urllib.request.Request(f"{url}/v1/channels")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 403

    def test_publish_and_read_round_trip_with_a_token(self, maildir_auth_server):
        _run_publish_round_trip(maildir_auth_server)


# ── NATS (skips without a reachable server) ─────────────────────────

NATS_URL = "nats://localhost:4222"


def _is_nats_available() -> bool:
    """Probe NATS_URL with a plain TCP connect, same approach as test_nats_backend.py."""
    parsed = urllib.parse.urlparse(NATS_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port or 4222
    try:
        sock = socket.create_connection((host, port), timeout=2)
        sock.close()
        return True
    except OSError:
        return False


@pytest.fixture()
def nats_auth_server(tmp_path):
    """NATS-backed server with a token store in its own SQLite file."""
    nats = pytest.importorskip("nats")  # noqa: F841
    from mansio.backends.nats import NATSBackend

    stream_name = f"MANSIO_AUTH_TEST_{int(time.time() * 1000)}"
    backend = NATSBackend(
        url=NATS_URL,
        stream_name=stream_name,
        subject_prefix=stream_name.lower(),
        storage="memory",
    )
    bus = Bus(backend=backend)
    token_store = TokenStore(str(tmp_path / "tokens.db"))

    url, server = _start_server(bus, token_store)
    yield url, token_store

    server.shutdown()
    bus.close()


@pytest.mark.skipif(not _is_nats_available(), reason=f"NATS server not available at {NATS_URL}")
class TestNatsBackendHasWorkingAuth:
    """A NATS-backed server enforces and honors token auth, per issue #285."""

    def test_rejects_requests_without_a_token(self, nats_auth_server):
        url, _token_store = nats_auth_server
        req = urllib.request.Request(f"{url}/v1/channels")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 403

    def test_publish_and_read_round_trip_with_a_token(self, nats_auth_server):
        _run_publish_round_trip(nats_auth_server)
