"""Wire-level delivery semantics matrix (target architecture v2, section 09).

Pins the three delivery guarantees and the presentation boundary on today's
behaviour, before the client Delivery refactor (M6f):

- Pull: cursor-based, at most once per message.
- Queue: lease-based, at least once until acknowledged.
- Stream: push-based, advisory; pull still sees messages missed by a stream.
- Format: the MCP result shapes, which carry no delivery semantics.

Only HTTP and the MCP stdio surface are observed; no client internals are
touched, so the tests should survive the refactor.  Stream warning-frame
overflow (the drop counter under buffer pressure) is intentionally not
covered here.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, NoReturn

import pytest
from mansio_client import MansioClient
from mansio_core.types import Message

A_ID = "claude-demo-1"
B_ID = "codex-demo-1"
PULL_CHANNEL = "pull-once"
READ_CHANNEL = "read-only"
QUEUE_CHANNEL = "queue-lane"
STREAM_CHANNEL = "stream-lane"
EMPTY_CHANNEL = "empty"
REQUEST_TIMEOUT = 10.0

# Lease is 1 s; wait a little past it without a long sleep.
LEASE_SECONDS = 1
LEASE_EXPIRY_WAIT_SECONDS = 1.3


class McpChild:
    """A ``python -m mansio_client.mcp`` child driven with line JSON-RPC."""

    def __init__(self, url: str, user_id: str, token: str) -> None:
        env = {
            **os.environ,
            "MANSIO_URL": url,
            "MANSIO_USER_ID": user_id,
            "MANSIO_TOKEN": token,
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mansio_client.mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        stream = self.proc.stdout
        if stream is None:
            self._lines.put(None)
            return
        try:
            for line in stream:
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)

    def _fail(self, message: str) -> NoReturn:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        stderr = self.proc.stderr.read() if self.proc.stderr is not None else ""
        raise AssertionError(f"{message}\nstderr tail:\n{stderr[-2000:]}")

    def _read(self, request_id: int, timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self._fail(f"no response to id {request_id} within {timeout}s")
        if line is None:
            self._fail(f"stdout closed before id {request_id}")
        return json.loads(line)

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        response = self._read(request_id)
        assert response.get("jsonrpc") == "2.0", response
        assert response.get("id") == request_id, response
        return response

    def initialize(self) -> None:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "delivery-semantics", "version": "1"},
            },
        )
        assert response["result"]["serverInfo"]["name"] == "mansio-mcp", response
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool(self, name: str, arguments: dict[str, Any]) -> Any:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "error" not in response, response
        result = response["result"]
        assert set(result) == {"content"}, result
        block = result["content"][0]
        assert block["type"] == "text"
        return json.loads(block["text"])

    def close(self) -> None:
        proc = self.proc
        if proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture()
def identities(mansio_server):
    """Two HTTP clients plus the URL and tokens for an MCP child."""
    url, token_store, _bus, _server = mansio_server
    tokens = {
        user_id: token_store.create_token(user_id=user_id, label=f"{user_id} delivery")["token"]
        for user_id in (A_ID, B_ID)
    }
    a = MansioClient(url, A_ID, token=tokens[A_ID])
    b = MansioClient(url, B_ID, token=tokens[B_ID])
    try:
        yield url, a, b, tokens
    finally:
        a.close()
        b.close()


# ── 1. Pull: at most once ───────────────────────────────────────────


def test_pull_delivers_each_message_at_most_once(identities) -> None:
    url, _a, b, tokens = identities
    b.channel_send(PULL_CHANNEL, "one")
    b.channel_send(PULL_CHANNEL, "two")

    child = McpChild(url, A_ID, tokens[A_ID])
    try:
        child.initialize()

        first = child.tool("mansio_poll", {"channel": PULL_CHANNEL, "include_own": True})
        assert isinstance(first, list), first
        assert [m["payload"] for m in first] == ["one", "two"], first
        assert [m["sender"] for m in first] == [B_ID, B_ID], first

        second = child.tool("mansio_poll", {"channel": PULL_CHANNEL, "include_own": True})
        assert second == [], second

        # Read and check_unread observe without consuming; pull still sees it.
        b.channel_send(READ_CHANNEL, "read-me")
        read = child.tool("mansio_read", {"channel": READ_CHANNEL})
        assert [m["payload"] for m in read] == ["read-me"], read
        unread = child.tool("mansio_check_unread", {})
        assert unread["unread"].get(READ_CHANNEL) == 1, unread
        assert unread["total_unread"] >= 1, unread

        still_there = child.tool("mansio_poll", {"channel": READ_CHANNEL})
        assert [m["payload"] for m in still_there] == ["read-me"], still_there

        consumed = child.tool("mansio_poll", {"channel": READ_CHANNEL})
        assert consumed == [], consumed
    finally:
        child.close()


# ── 2. Queue: at least once ─────────────────────────────────────────


def test_queue_redelivers_until_acknowledged(identities) -> None:
    _url, a, b, _tokens = identities
    message_id = b.queue_publish(QUEUE_CHANNEL, "job")

    pending = a.queue_status(message_id)
    assert pending is not None
    assert pending["status"] == "unclaimed", pending
    assert pending["claimed_by"] is None, pending

    first = a.queue_claim(QUEUE_CHANNEL, lease_seconds=LEASE_SECONDS)
    assert first is not None
    assert first.id == message_id
    assert first.claimed_by == A_ID
    claimed = a.queue_status(message_id)
    assert claimed is not None
    assert claimed["status"] == "claimed", claimed
    assert claimed["claimed_by"] == A_ID, claimed
    assert claimed["lease_until"], claimed

    # The lease expires; an unacknowledged message goes to the next claimer.
    time.sleep(LEASE_EXPIRY_WAIT_SECONDS)
    second = b.queue_claim(QUEUE_CHANNEL, lease_seconds=LEASE_SECONDS)
    assert second is not None
    assert second.id == message_id, "an expired lease must redeliver the same message"
    re_claimed = b.queue_status(message_id)
    assert re_claimed is not None
    assert re_claimed["status"] == "claimed", re_claimed
    assert re_claimed["claimed_by"] == B_ID, re_claimed

    ack = b.queue_ack(message_id)
    assert ack is not None
    assert ack.status == "completed"
    completed = b.queue_status(message_id)
    assert completed is not None
    assert completed["status"] == "completed", completed
    assert completed["claimed_by"] == B_ID, completed

    assert a.queue_claim(QUEUE_CHANNEL, lease_seconds=LEASE_SECONDS) is None


# ── 3. Stream: advisory ─────────────────────────────────────────────


def test_stream_is_advisory_and_pull_sees_messages_missed_while_down(identities) -> None:
    _url, a, b, _tokens = identities
    received: list[Message] = []
    arrived = threading.Event()

    def on_message(message: Message) -> None:
        received.append(message)
        arrived.set()

    subscription = a.subscribe(STREAM_CHANNEL, on_message)
    try:
        # Let the SSE request connect before publishing; the stream is
        # advisory, so a message published before it is live would be missed
        # by design.  Same handshake as test_remote_transport's SSE test.
        time.sleep(0.5)
        b.channel_send(STREAM_CHANNEL, "streamed")
        assert arrived.wait(REQUEST_TIMEOUT), "the SSE stream did not push the message"
        assert [m.payload for m in received] == ["streamed"], received
    finally:
        a.unsubscribe(subscription)

    # With no stream open, the message still lands in the store and pull sees it.
    b.channel_send(STREAM_CHANNEL, "while-down")
    polled = a.channel_poll(STREAM_CHANNEL, include_own=True)
    assert "while-down" in [m.payload for m in polled], polled


# ── 4. Format boundary ───────────────────────────────────────────────


def test_format_boundary_keeps_wait_and_poll_shapes(identities) -> None:
    url, _a, _b, tokens = identities
    child = McpChild(url, A_ID, tokens[A_ID])
    try:
        child.initialize()

        wait = child.tool("mansio_wait", {"channel": EMPTY_CHANNEL, "timeout_seconds": 0})
        assert set(wait) == {"messages", "timed_out"}, wait
        assert wait == {"messages": [], "timed_out": True}, wait

        poll = child.tool("mansio_poll", {"channel": EMPTY_CHANNEL})
        assert isinstance(poll, list), poll
        assert poll == [], poll
    finally:
        child.close()
