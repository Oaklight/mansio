"""Tests for ``mansio-agent doctor`` against a loopback stub server."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from mansio_client.agent import doctor

TOKEN = "secret-token-value"


class _Handler(BaseHTTPRequestHandler):
    server: _StubServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.server.expected_token is None:
            return True
        return self.headers.get("Authorization") == f"Bearer {self.server.expected_token}"

    def do_GET(self) -> None:
        if self.path == "/v1/auth/check":
            return self._json({"auth_mode": self.server.auth_mode, "has_tokens": False})
        if self.path == "/v1/channels":
            if not self._authorized():
                return self._json({"error": "Unauthorized"}, 401)
            return self._json({"channels": ["general", "inbox"]})
        if self.path.startswith("/v1/presence"):
            if not self._authorized():
                return self._json({"error": "Unauthorized"}, 401)
            return self._json(
                {
                    "agents": [
                        {
                            "user_id": "agent-1",
                            "status": "online",
                            "last_seen": "2026-01-01T00:00:00Z",
                            "metadata": None,
                        }
                    ]
                }
            )
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path == "/v1/presence/heartbeat":
            if not self._authorized():
                return self._json({"error": "Unauthorized"}, 401)
            return self._json({})
        return self._json({"error": "not found"}, 404)


class _StubServer(ThreadingHTTPServer):
    auth_mode = "disabled"
    expected_token: str | None = None


@pytest.fixture()
def server() -> Iterator[_StubServer]:
    instance = _StubServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _fake_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.sys, "argv", ["/usr/bin/mansio-agent"])


def _url(server: _StubServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def test_doctor_ok(server: _StubServer) -> None:
    server.auth_mode = "required"
    server.expected_token = TOKEN
    lines: list[str] = []

    code = doctor.run(url=_url(server), user_id="agent-1", token=TOKEN, out=lines.append)

    assert code == 0
    text = "\n".join(lines)
    assert "[ok] mansio-mcp" in text
    assert "[ok] mansio-agent" in text
    assert "[ok] reachable" in text
    assert "[ok] auth: required" in text
    assert "[ok] channels: 2 visible" in text
    assert "[ok] heartbeat" in text
    assert "[ok] agent roster (mansio_agents): 1 agents" in text
    assert TOKEN not in text


def test_doctor_disabled_auth(server: _StubServer) -> None:
    lines: list[str] = []
    code = doctor.run(url=_url(server), user_id="agent-1", token=None, out=lines.append)
    assert code == 0
    assert "[ok] auth: disabled" in "\n".join(lines)


def test_doctor_reports_channel_auth_failure(server: _StubServer) -> None:
    server.auth_mode = "required"
    server.expected_token = "right-token"
    lines: list[str] = []

    code = doctor.run(url=_url(server), user_id="agent-1", token="wrong-token", out=lines.append)

    assert code == 1
    text = "\n".join(lines)
    assert "[FAIL] channels" in text
    assert "401" in text


def test_doctor_missing_token(server: _StubServer) -> None:
    server.auth_mode = "required"
    server.expected_token = TOKEN
    lines: list[str] = []

    code = doctor.run(url=_url(server), user_id="agent-1", token=None, out=lines.append)

    assert code == 1
    text = "\n".join(lines)
    assert "[FAIL] token" in text
    assert "export MANSIO_TOKEN" in text


def test_doctor_unreachable() -> None:
    lines: list[str] = []
    code = doctor.run(url="http://127.0.0.1:1", user_id="agent-1", token=None, out=lines.append)
    assert code == 1
    assert "[FAIL] reachable" in "\n".join(lines)


def test_doctor_missing_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    lines: list[str] = []
    code = doctor.run(url=None, user_id=None, token=None, out=lines.append)
    assert code == 1
    text = "\n".join(lines)
    assert "[FAIL] mansio-mcp" in text
    assert "[FAIL] mansio-agent" in text


def test_doctor_missing_url() -> None:
    lines: list[str] = []
    code = doctor.run(url=None, user_id="agent-1", token=None, out=lines.append)
    assert code == 1
    assert "[FAIL] url" in "\n".join(lines)


def test_doctor_notes_codex_hook_review(tmp_path: Path, server: _StubServer) -> None:
    hooks = tmp_path / ".codex" / "hooks.json"
    hooks.parent.mkdir()
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup|resume",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "mansio-agent poll --format codex",
                                    "timeout": 15,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    lines: list[str] = []
    code = doctor.run(
        url=_url(server), user_id="agent-1", token=None, out=lines.append, cwd=tmp_path
    )
    assert code == 0
    assert "[note] Codex: review the installed hook with /hooks" in "\n".join(lines)
