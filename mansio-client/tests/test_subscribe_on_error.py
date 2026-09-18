"""Tests for MansioClient.subscribe() forwarding the on_error hook (issue #304).

HttpTransport.subscribe() has accepted an on_error(message, exception) hook
since PR #283, but MansioClient.subscribe() did not expose it, so callers
of the client SDK had no programmatic way to react to callback failures.

Verifies:
- MansioClient.subscribe() accepts an on_error argument
- on_error is forwarded to the underlying transport and invoked on
  callback failure, with the failing message and the raised exception
- Omitting on_error still works (backward compatible default)
"""

from __future__ import annotations

import threading
import time

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend
from mansio_client import MansioClient


@pytest.fixture()
def server_url():
    """Start a MansioServer with HttpFrontend on a random port, yield URL."""
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0, allow_unauthenticated=True)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    yield f"http://{host}:{port}"

    server.shutdown()


class TestSubscribeOnError:
    """MansioClient.subscribe() must plumb on_error through to the transport."""

    def test_on_error_hook_receives_failure(self, server_url: str) -> None:
        """A callback that raises must have its exception routed to on_error."""
        with MansioClient(server_url, "hook-agent") as client:
            failures: list[tuple] = []
            reported = threading.Event()

            def boom(msg) -> None:
                raise ValueError("client-level failure")

            def on_error(msg, exc) -> None:
                failures.append((msg, exc))
                reported.set()

            client.subscribe("client-hook-ch", boom, on_error)
            time.sleep(0.5)
            client.channel_send("client-hook-ch", "trigger")

            assert reported.wait(timeout=5), "on_error was never called"

            msg, exc = failures[0]
            assert msg.channel == "client-hook-ch"
            assert msg.payload == "trigger"
            assert isinstance(exc, ValueError)
            assert str(exc) == "client-level failure"

    def test_subscribe_without_on_error_still_works(self, server_url: str) -> None:
        """Omitting on_error must remain valid — it's an optional hook."""
        with MansioClient(server_url, "no-hook-agent") as client:
            received: list = []
            got_it = threading.Event()

            def on_msg(msg) -> None:
                received.append(msg)
                got_it.set()

            sub_id = client.subscribe("client-no-hook-ch", on_msg)
            assert sub_id

            time.sleep(0.5)
            client.channel_send("client-no-hook-ch", "hello")

            assert got_it.wait(timeout=5), "callback never ran"
            assert received[0].payload == "hello"
