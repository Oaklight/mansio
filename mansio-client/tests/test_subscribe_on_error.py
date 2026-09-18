"""Tests for MansioClient.subscribe() forwarding the on_error hook (issue #304).

HttpTransport.subscribe() has accepted an on_error(message, exception) hook
since PR #283, but MansioClient.subscribe() did not expose it, so callers
of the client SDK had no programmatic way to react to callback failures.

mansio-client is published and tested standalone, without the mansio
server package installed (see the client-check CI job), so these tests
drive MansioClient against a small stdlib http.server stub that speaks
just enough of the wire protocol (JSON POST/GET + one SSE frame) rather
than a real mansio server.

Verifies:
- MansioClient.subscribe() accepts an on_error argument
- on_error is forwarded to the underlying transport and invoked, with the
  failing message and the raised exception, when the callback raises
- Omitting on_error still works (backward compatible default)
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mansio_client import MansioClient


class _StubHandler(BaseHTTPRequestHandler):
    """Answers MansioClient's bootstrap calls and pushes one canned SSE
    message for whatever channel the client subscribes to.

    Real message content (sender, timestamps, etc.) doesn't matter here —
    only that on_error forwarding works — so every response is a fixed
    stand-in rather than anything resembling real bus state.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass  # Keep test output free of per-request access logs.

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)  # Drain the request body.
        if self.path == "/v1/publish":
            self._write_json(200, {"message_id": "stub-msg-id"})
            return
        self._write_json(404, {"error": "not found"})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/v1/query":
            self._write_json(200, {"messages": []})
            return
        if parsed.path == "/v1/subscribe":
            self._serve_sse(parsed)
            return
        self._write_json(404, {"error": "not found"})

    def _serve_sse(self, parsed: urllib.parse.ParseResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        channel = params.get("channel", ["stub-channel"])[0]
        event = {
            "channel": channel,
            "message": {
                "id": "stub-msg-1",
                "channel": channel,
                "sender": "stub-sender",
                "msg_type": "chat",
                "payload": "trigger",
                "timestamp": "2024-01-01T00:00:00Z",
            },
        }
        body = f"data: {json.dumps(event)}\n\n".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        # Tell the server framework — not just the client — to actually
        # close the socket, matching the "Connection: close" header
        # above. Without this the client waits for EOF that never comes.
        self.close_connection = True


@pytest.fixture()
def stub_server():
    """Start the stdlib stub server on a random port, yield its URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server.server_address
    yield f"http://{host}:{port}"

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class TestSubscribeOnError:
    """MansioClient.subscribe() must plumb on_error through to the transport."""

    def test_on_error_hook_receives_failure(self, stub_server: str) -> None:
        """A callback that raises must have its exception routed to on_error."""
        with MansioClient(stub_server, "hook-agent") as client:
            failures: list[tuple] = []
            reported = threading.Event()

            def boom(msg) -> None:
                raise ValueError("client-level failure")

            def on_error(msg, exc) -> None:
                failures.append((msg, exc))
                reported.set()

            client.subscribe("client-hook-ch", boom, on_error)

            assert reported.wait(timeout=5), "on_error was never called"

        msg, exc = failures[0]
        assert msg.channel == "client-hook-ch"
        assert msg.payload == "trigger"
        assert isinstance(exc, ValueError)
        assert str(exc) == "client-level failure"

    def test_subscribe_without_on_error_still_works(self, stub_server: str) -> None:
        """Omitting on_error must remain valid — it's an optional hook."""
        with MansioClient(stub_server, "no-hook-agent") as client:
            received: list = []
            got_it = threading.Event()

            def on_msg(msg) -> None:
                received.append(msg)
                got_it.set()

            sub_id = client.subscribe("client-no-hook-ch", on_msg)
            assert sub_id

            assert got_it.wait(timeout=5), "callback never ran"

        assert received[0].channel == "client-no-hook-ch"
        assert received[0].payload == "trigger"
