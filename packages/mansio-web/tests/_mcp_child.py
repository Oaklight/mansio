"""Shared driver for a ``python -m mansio_client.mcp`` child over stdio.

Used by ``test_agent_entry_wait.py`` and ``test_delivery_semantics.py``.  The
canonical ``test_agent_entry_conformance.py`` deliberately keeps its own copy:
it is the reference conformance test and is not refactored by this helper.

The reader thread plus queue avoids ``select`` on a ``TextIOWrapper``:
``readline`` can buffer the next line, which would make ``select`` on the raw
fd miss a response that is already in Python's buffer.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
from typing import Any, NoReturn

REQUEST_TIMEOUT = 10.0


class McpChild:
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
        self._lines: queue.Queue[str | None] = queue.Queue()
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

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _fail(self, message: str) -> NoReturn:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        stderr = self.proc.stderr.read() if self.proc.stderr is not None else ""
        raise AssertionError(f"{self.user_id}: {message}\nstderr tail:\n{stderr[-2000:]}")

    def _read(self, request_id: int, timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self._fail(f"no response to id {request_id} within {timeout}s")
        if line is None:
            self._fail(f"stdout closed before id {request_id}")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            self._fail(f"non-JSON line before id {request_id}: {line!r} ({exc})")

    def expect_no_line(self, seconds: float, context: str) -> None:
        """Fail with the exact line if the server answers within *seconds*."""
        try:
            line = self._lines.get(timeout=seconds)
        except queue.Empty:
            return
        if line is None:
            raise AssertionError(f"{context}: stdout closed")
        raise AssertionError(f"{context}: server answered early: {line!r}")

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
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
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> None:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mansio-conformance", "version": "1"},
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

    def tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Alias for :meth:`call_json`, used by the delivery tests."""
        return self.call_json(name, arguments)

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
