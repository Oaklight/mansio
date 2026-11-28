"""Conformance: two independent agent runtimes cross-talk through MANSIO.

This is the acceptance test for the agent entry contract:

- a real HTTP server with token auth and scoped (non-supertoken) identities;
- two separate ``mansio-mcp`` processes over stdio, configured the way the
  adapter examples document (``MANSIO_URL`` / ``MANSIO_USER_ID`` /
  ``MANSIO_TOKEN``);
- the documented tool calls in both directions (plain channel and DM);
- presence via ``mansio_heartbeat`` / ``mansio_agents``;
- isolation from a third identity that holds no ACL on the DM.

Every JSON-RPC envelope and tool payload shape is pinned, so a wire change
fails here rather than in an adapter.

The MCP entry is started with ``sys.executable -m mansio_client.mcp``, the
documented module entry, so the test also proves that the module itself is
runnable (the two-line ``__main__`` guard added with this test; issue #410).
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import subprocess
import sys
from typing import Any, NoReturn

import pytest
from mansio_client import MansioAPIError, MansioClient

A_ID = "claude-demo-1"
B_ID = "codex-demo-1"
C_ID = "guest-demo-1"
CHANNEL = "agents"
DM_CHANNEL = f"dm:{A_ID}:{B_ID}"
PUBLIC_TEXT = "hello-from-claude"
DM_TEXT = "codex-private-marker"

# Pinned wire shapes.  These are the values the MCP server returns today.
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "mansio-mcp", "version": "0.1.0"}
MESSAGE_KEYS = {"id", "channel", "sender", "msg_type", "payload", "timestamp"}
PRESENCE_KEYS = {"user_id", "status", "last_seen", "metadata"}

# Bounded per-request wait.  The server answers synchronously; this only
# guards against a hung or crashing child.
REQUEST_TIMEOUT = 10.0


def _require_mcp_entry() -> None:
    """The documented module entry must run; fail readably, not by hanging."""
    check = subprocess.run(
        [sys.executable, "-m", "mansio_client.mcp", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if check.returncode != 0:
        pytest.fail(
            f"`{sys.executable} -m mansio_client.mcp` failed (rc={check.returncode}); "
            f"the conformance test needs the client package installed:\n{check.stderr.strip()}"
        )
    assert "Mansio MCP server" in check.stdout, check.stdout


class McpProcess:
    """One independent agent runtime: a mansio-mcp stdio server."""

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

    # ── transport ────────────────────────────────────────────────

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

    # ── JSON-RPC ─────────────────────────────────────────────────

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        response = self._read(request_id, timeout)
        assert response.get("jsonrpc") == "2.0", response
        assert response.get("id") == request_id, response
        return response

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> None:
        response = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mansio-conformance", "version": "1"},
            },
        )
        assert response["result"] == {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }, response
        self.notify("notifications/initialized")

    def call_raw(self, tool: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Call a tool and validate the JSON-RPC envelope.

        Returns the ``result`` object and its single text block.  An error
        result is allowed here so isolation probes can inspect it.
        """
        response = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert "error" not in response, response
        assert set(response) == {"jsonrpc", "id", "result"}, response
        result = response["result"]
        assert set(result) <= {"content", "isError"}, result
        assert set(result) >= {"content"}, result
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


def _assert_message(message: dict[str, Any], *, channel: str, sender: str, payload: str) -> None:
    assert set(message) == MESSAGE_KEYS, message
    assert message["channel"] == channel
    assert message["sender"] == sender
    assert message["payload"] == payload
    assert message["msg_type"] == "chat"
    assert message["id"]
    assert message["timestamp"]


def test_two_agent_runtimes_cross_talk(mansio_server) -> None:
    url, token_store, _bus, _server = mansio_server
    tokens = {
        user_id: token_store.create_token(user_id=user_id, label=f"{user_id} conformance")["token"]
        for user_id in (A_ID, B_ID, C_ID)
    }

    _require_mcp_entry()

    a = McpProcess(url, A_ID, tokens[A_ID])
    b = McpProcess(url, B_ID, tokens[B_ID])
    guest: MansioClient | None = None
    try:
        a.initialize()
        b.initialize()

        # ── A → plain channel → B ────────────────────────────────
        sent = a.call_json("mansio_send", {"channel": CHANNEL, "content": PUBLIC_TEXT})
        assert set(sent) == {"message_id"}, sent
        polled = b.call_json("mansio_poll", {"channel": CHANNEL})
        assert len(polled) == 1, polled
        _assert_message(polled[0], channel=CHANNEL, sender=A_ID, payload=PUBLIC_TEXT)
        assert polled[0]["id"] == sent["message_id"]

        # ── B → DM → A ───────────────────────────────────────────
        dm_sent = b.call_json("mansio_dm_send", {"to_user": A_ID, "content": DM_TEXT})
        assert set(dm_sent) == {"message_id"}, dm_sent
        read = a.call_json("mansio_read", {"channel": DM_CHANNEL})
        assert len(read) == 1, read
        _assert_message(read[0], channel=DM_CHANNEL, sender=B_ID, payload=DM_TEXT)
        assert read[0]["id"] == dm_sent["message_id"]

        # ── presence ─────────────────────────────────────────────
        assert a.call_json("mansio_heartbeat", {}) == {"status": "ok"}
        assert b.call_json("mansio_heartbeat", {}) == {"status": "ok"}
        agents = a.call_json("mansio_agents", {})
        by_id = {agent["user_id"]: agent for agent in agents}
        assert set(by_id) == {A_ID, B_ID}, by_id
        for user_id in (A_ID, B_ID):
            assert set(by_id[user_id]) == PRESENCE_KEYS, by_id[user_id]
            assert by_id[user_id]["status"] == "online"

        # ── isolation: C holds no ACL on the DM ──────────────────
        guest = MansioClient(url, C_ID, token=tokens[C_ID])
        assert DM_CHANNEL not in guest.channel_list()
        try:
            guest_view: list[dict[str, Any]] = [
                {"id": m.id, "payload": m.payload} for m in guest.channel_read(DM_CHANNEL)
            ]
        except MansioAPIError:
            # The server denies the read; the point is that no message is
            # exposed, not which wording the denial uses.
            guest_view = []
        assert guest_view == []
        assert DM_TEXT not in json.dumps(guest_view)
    finally:
        a.close()
        b.close()
        if guest is not None:
            guest.close()
