"""Conformance for ``mansio_wait``: blocking wait, timeout, errors, cursor.

Companion to ``test_agent_entry_conformance.py``; it deliberately keeps its
own small JSON-RPC helper so the canonical test stays untouched.  One real
server, one ``python -m mansio_client.mcp`` child (A) driven over stdio, and
an in-process publisher (B) whose sends race the wait.

Pinned contract (mirrors ``_tool_wait`` on master):

- ``{"channel": ..., "timeout_seconds": N, "include_own": bool}``;
- success: ``{"messages": [...], "timed_out": false}``;
- deadline: ``{"messages": [], "timed_out": true}``;
- ``timeout_seconds`` above 120 or below 0 is a JSON-RPC error, code -32602;
- the wait polls through ``channel_poll``, so it shares that cursor.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import subprocess
import sys
import time
from typing import Any, NoReturn

import pytest
from mansio_client import MansioClient

A_ID = "claude-demo-1"
B_ID = "codex-demo-1"
CHANNEL = "agents"
EMPTY_CHANNEL = "empty"
PUBLIC_TEXT = "published-during-wait"
REQUEST_TIMEOUT = 10.0
# How long to watch the child before publishing; a readable line here means
# the wait returned early, which is the bug the blocking proof looks for.
BLOCK_PROBE_SECONDS = 0.3

MESSAGE_KEYS = {"id", "channel", "sender", "msg_type", "payload", "timestamp"}
WAIT_MAX_SECONDS = 120


def _require_mcp_entry() -> None:
    check = subprocess.run(
        [sys.executable, "-m", "mansio_client.mcp", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if check.returncode != 0:
        pytest.fail(
            f"`{sys.executable} -m mansio_client.mcp` failed (rc={check.returncode}); "
            f"the wait test needs the client package installed:\n{check.stderr.strip()}"
        )
    assert "Mansio MCP server" in check.stdout, check.stdout


class McpProcess:
    """One ``mansio-mcp`` child, driven with line-delimited JSON-RPC."""

    def __init__(self, url: str, user_id: str, token: str) -> None:
        env = {
            **os.environ,
            "MANSIO_URL": url,
            "MANSIO_USER_ID": user_id,
            "MANSIO_TOKEN": token,
        }
        self.user_id = user_id
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mansio_client.mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._next_id = 0

    def _write(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _fail(self, message: str) -> NoReturn:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        stderr = self.proc.stderr.read() if self.proc.stderr is not None else ""
        raise AssertionError(f"{self.user_id}: {message}\nstderr tail:\n{stderr[-2000:]}")

    def _read(self, request_id: int, timeout: float) -> dict[str, Any]:
        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            self._fail(f"no response to id {request_id} within {timeout}s")
        line = self.proc.stdout.readline()
        if not line:
            self._fail(f"stdout closed before id {request_id}")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            self._fail(f"non-JSON line before id {request_id}: {line!r} ({exc})")

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._next_id += 1
        request_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return request_id

    def read_response(self, request_id: int, timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        response = self._read(request_id, timeout)
        assert response.get("jsonrpc") == "2.0", response
        assert response.get("id") == request_id, response
        return response

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        return self.read_response(self.send_request(method, params), timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> None:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mansio-wait-conformance", "version": "1"},
            },
        )
        assert response["result"]["serverInfo"]["name"] == "mansio-mcp", response
        self.notify("notifications/initialized")

    def call_raw(self, tool: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        response = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert "error" not in response, response
        assert set(response) == {"jsonrpc", "id", "result"}, response
        result = response["result"]
        assert set(result) <= {"content", "isError"}, result
        content = result["content"]
        assert isinstance(content, list) and len(content) == 1, content
        block = content[0]
        assert set(block) == {"type", "text"}, block
        assert block["type"] == "text"
        return result, block["text"]

    def call_json(self, tool: str, arguments: dict[str, Any]) -> Any:
        result, text = self.call_raw(tool, arguments)
        assert set(result) == {"content"}, f"{tool} returned an error: {text}"
        return json.loads(text)

    def expect_rpc_error(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.request(method, params)
        assert set(response) == {"jsonrpc", "id", "error"}, response
        return response["error"]

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


def _assert_message(message: dict[str, Any], *, sender: str, payload: str) -> None:
    assert set(message) == MESSAGE_KEYS, message
    assert message["channel"] == CHANNEL
    assert message["sender"] == sender
    assert message["payload"] == payload
    assert message["msg_type"] == "chat"
    assert message["id"]
    assert message["timestamp"]


@pytest.fixture()
def runtimes(mansio_server):
    """One MCP child (A) and one in-process publisher (B)."""
    url, token_store, _bus, _server = mansio_server
    tokens = {
        user_id: token_store.create_token(user_id=user_id, label=f"{user_id} wait")["token"]
        for user_id in (A_ID, B_ID)
    }
    _require_mcp_entry()
    a = McpProcess(url, A_ID, tokens[A_ID])
    b = MansioClient(url, B_ID, token=tokens[B_ID])
    try:
        a.initialize()
        yield a, b
    finally:
        a.close()
        b.close()


def test_message_published_during_the_wait_returns_before_the_deadline(runtimes) -> None:
    a, b = runtimes
    sent_at = time.monotonic()
    request_id = a.send_request(
        "tools/call",
        {"name": "mansio_wait", "arguments": {"channel": CHANNEL, "timeout_seconds": 2}},
    )

    # The wait must still be blocking before the publish.  Surface the exact
    # line instead of a bare timeout if it answered early.
    assert a.proc.stdout is not None
    ready, _, _ = select.select([a.proc.stdout], [], [], BLOCK_PROBE_SECONDS)
    if ready:
        early = a.proc.stdout.readline()
        raise AssertionError(f"mansio_wait answered before B published: {early!r}")

    b.channel_send(CHANNEL, PUBLIC_TEXT)
    response = a.read_response(request_id, timeout=REQUEST_TIMEOUT)
    elapsed = time.monotonic() - sent_at

    assert "error" not in response, response
    result = response["result"]
    assert set(result) == {"content"}, result
    payload = json.loads(result["content"][0]["text"])
    assert set(payload) == {"messages", "timed_out"}, payload
    assert payload["timed_out"] is False
    assert len(payload["messages"]) == 1, payload
    _assert_message(payload["messages"][0], sender=B_ID, payload=PUBLIC_TEXT)
    assert elapsed < 2.0, f"wait returned after its deadline: {elapsed:.2f}s"


def test_zero_timeout_returns_immediately(runtimes) -> None:
    a, _b = runtimes
    started = time.monotonic()
    result = a.call_json("mansio_wait", {"channel": EMPTY_CHANNEL, "timeout_seconds": 0})
    elapsed = time.monotonic() - started
    assert result == {"messages": [], "timed_out": True}
    assert elapsed < 1.0, f"zero timeout slept: {elapsed:.2f}s"


def test_small_timeout_is_bounded_by_the_deadline(runtimes) -> None:
    a, _b = runtimes
    started = time.monotonic()
    result = a.call_json("mansio_wait", {"channel": EMPTY_CHANNEL, "timeout_seconds": 1})
    elapsed = time.monotonic() - started
    assert result == {"messages": [], "timed_out": True}
    assert elapsed >= 0.9, f"wait returned before the timeout: {elapsed:.2f}s"
    assert elapsed < 4.0, f"wait overran the timeout: {elapsed:.2f}s"


def test_default_timeout_is_accepted_and_returns_a_pending_message(runtimes) -> None:
    a, b = runtimes
    b.channel_send(CHANNEL, PUBLIC_TEXT)
    result = a.call_json("mansio_wait", {"channel": CHANNEL})
    assert set(result) == {"messages", "timed_out"}, result
    assert result["timed_out"] is False
    assert [m["payload"] for m in result["messages"]] == [PUBLIC_TEXT]


def test_timeout_above_the_maximum_is_a_json_rpc_error(runtimes) -> None:
    a, _b = runtimes
    error = a.expect_rpc_error(
        "tools/call",
        {"name": "mansio_wait", "arguments": {"channel": CHANNEL, "timeout_seconds": 121}},
    )
    assert error == {
        "code": -32602,
        "message": f"timeout_seconds must be at most {WAIT_MAX_SECONDS}",
    }, error


def test_negative_timeout_is_a_json_rpc_error(runtimes) -> None:
    a, _b = runtimes
    error = a.expect_rpc_error(
        "tools/call",
        {"name": "mansio_wait", "arguments": {"channel": CHANNEL, "timeout_seconds": -1}},
    )
    assert error == {
        "code": -32602,
        "message": "timeout_seconds must be non-negative",
    }, error


def test_wait_and_poll_share_one_cursor(runtimes) -> None:
    a, b = runtimes

    # Wait consumes; poll does not see it again.
    b.channel_send(CHANNEL, "first")
    waited = a.call_json("mansio_wait", {"channel": CHANNEL, "timeout_seconds": 0})
    assert [m["payload"] for m in waited["messages"]] == ["first"]
    assert a.call_json("mansio_poll", {"channel": CHANNEL}) == []

    # Poll consumes; wait does not see it again.
    b.channel_send(CHANNEL, "second")
    assert [m["payload"] for m in a.call_json("mansio_poll", {"channel": CHANNEL})] == ["second"]
    assert a.call_json("mansio_wait", {"channel": CHANNEL, "timeout_seconds": 0}) == {
        "messages": [],
        "timed_out": True,
    }
