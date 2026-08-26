"""Tests for the client-side message injection layer.

Covers the Injector protocol and all four concrete implementations:
MailboxInjector, ClaudeCodeInjector, OpenClawInjector, WebhookInjector.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import MagicMock, patch

from mansio_client.injectors import (
    ClaudeCodeInjector,
    Injector,
    MailboxInjector,
    OpenClawInjector,
    WebhookInjector,
    _msg_to_dict,
    _safe_filename,
)
from mansio_client.types import Message

# ── Fixtures ──────────────────────────────────────────────────────


def _make_msg(
    *,
    msg_id: str = "01234567-0000-0000-0000-000000000000",
    channel: str = "general",
    sender: str = "agent-a",
    msg_type: str = "chat",
    payload: str = "hello world",
    timestamp: str = "2026-08-26T20:00:00Z",
    metadata: dict | None = None,
    parent_id: str | None = None,
    thread_id: str | None = None,
    intent: str | None = None,
) -> Message:
    return Message(
        id=msg_id,
        channel=channel,
        sender=sender,
        msg_type=msg_type,
        payload=payload,
        timestamp=timestamp,
        metadata=metadata,
        parent_id=parent_id,
        thread_id=thread_id,
        intent=intent,
    )


# ── Protocol Conformance ─────────────────────────────────────────


class TestInjectorProtocol:
    """Verify that all injector classes satisfy the Injector protocol."""

    def test_mailbox_is_injector(self, tmp_path):
        injector = MailboxInjector(str(tmp_path / "mail.jsonl"))
        assert isinstance(injector, Injector)

    def test_claude_code_is_injector(self, tmp_path):
        injector = ClaudeCodeInjector(str(tmp_path))
        assert isinstance(injector, Injector)

    def test_openclaw_is_injector(self, tmp_path):
        injector = OpenClawInjector(str(tmp_path / "memory"))
        assert isinstance(injector, Injector)

    def test_webhook_is_injector(self):
        injector = WebhookInjector("http://localhost:9999/hook")
        assert isinstance(injector, Injector)


# ── MailboxInjector ───────────────────────────────────────────────


class TestMailboxInjector:
    def test_inject_creates_file(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        injector = MailboxInjector(path)
        msg = _make_msg()

        injector.inject(msg)

        assert os.path.isfile(path)
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["sender"] == "agent-a"
        assert data["payload"] == "hello world"
        assert data["channel"] == "general"

    def test_inject_appends(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        injector = MailboxInjector(path)

        injector.inject(_make_msg(payload="first"))
        injector.inject(_make_msg(payload="second"))

        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["payload"] == "first"
        assert json.loads(lines[1])["payload"] == "second"

    def test_inject_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "inbox.jsonl")
        injector = MailboxInjector(path)

        injector.inject(_make_msg())

        assert os.path.isfile(path)

    def test_rotation(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        injector = MailboxInjector(path, max_lines=10)

        # Write exactly 11 to trigger rotation once at line 11
        for i in range(11):
            injector.inject(_make_msg(payload=f"msg-{i}"))

        with open(path) as f:
            lines = f.readlines()
        # max_lines=10, rotation keeps max_lines//2 = 5
        assert len(lines) == 5
        # Should have the last 5 messages
        assert json.loads(lines[0])["payload"] == "msg-6"
        assert json.loads(lines[-1])["payload"] == "msg-10"

    def test_no_rotation_when_disabled(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        injector = MailboxInjector(path, max_lines=0)

        for i in range(20):
            injector.inject(_make_msg(payload=f"msg-{i}"))

        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 20

    def test_existing_file_line_count(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        # Pre-populate with 5 lines
        with open(path, "w") as f:
            for i in range(5):
                f.write(json.dumps({"payload": f"old-{i}"}) + "\n")

        injector = MailboxInjector(path, max_lines=8)
        assert injector._line_count == 5

        # Add 4 more → 9 total, triggers rotation at line 9 > 8
        for i in range(4):
            injector.inject(_make_msg(payload=f"new-{i}"))

        # 9 > max_lines=8, should rotate to keep 4
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 4

    def test_metadata_and_threading_preserved(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        injector = MailboxInjector(path)
        msg = _make_msg(
            metadata={"key": "value"},
            parent_id="parent-123",
            thread_id="thread-456",
        )

        injector.inject(msg)

        with open(path) as f:
            data = json.loads(f.readline())
        assert data["metadata"] == {"key": "value"}
        assert data["parent_id"] == "parent-123"
        assert data["thread_id"] == "thread-456"

    def test_intent_preserved(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        injector = MailboxInjector(path)
        msg = _make_msg(intent="summarize")

        injector.inject(msg)

        with open(path) as f:
            data = json.loads(f.readline())
        assert data["intent"] == "summarize"

    def test_intent_omitted_when_none(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        injector = MailboxInjector(path)
        msg = _make_msg()  # intent defaults to None

        injector.inject(msg)

        with open(path) as f:
            data = json.loads(f.readline())
        assert "intent" not in data

    def test_close_is_noop(self, tmp_path):
        injector = MailboxInjector(str(tmp_path / "inbox.jsonl"))
        injector.close()  # should not raise

    def test_path_property(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        injector = MailboxInjector(path)
        assert injector.path == path

    def test_unicode_payload(self, tmp_path):
        path = str(tmp_path / "inbox.jsonl")
        injector = MailboxInjector(path)

        injector.inject(_make_msg(payload="你好世界 🌍"))

        with open(path, encoding="utf-8") as f:
            data = json.loads(f.readline())
        assert data["payload"] == "你好世界 🌍"


# ── ClaudeCodeInjector ────────────────────────────────────────────


class TestClaudeCodeInjector:
    def test_default_path(self, tmp_path):
        injector = ClaudeCodeInjector(str(tmp_path))
        expected = os.path.join(str(tmp_path), ".claude", "mansio-inbox.jsonl")
        assert injector.path == expected

    def test_inject_creates_claude_dir(self, tmp_path):
        injector = ClaudeCodeInjector(str(tmp_path))

        injector.inject(_make_msg())

        assert os.path.isdir(os.path.join(str(tmp_path), ".claude"))
        assert os.path.isfile(injector.path)

    def test_inherits_mailbox_behavior(self, tmp_path):
        injector = ClaudeCodeInjector(str(tmp_path))

        injector.inject(_make_msg(payload="test"))

        with open(injector.path) as f:
            data = json.loads(f.readline())
        assert data["payload"] == "test"

    def test_default_max_lines(self, tmp_path):
        injector = ClaudeCodeInjector(str(tmp_path))
        assert injector._max_lines == 500

    def test_custom_max_lines(self, tmp_path):
        injector = ClaudeCodeInjector(str(tmp_path), max_lines=100)
        assert injector._max_lines == 100


# ── OpenClawInjector ──────────────────────────────────────────────


class TestOpenClawInjector:
    def test_inject_creates_md_file(self, tmp_path):
        directory = str(tmp_path / "mansio")
        injector = OpenClawInjector(directory)

        injector.inject(_make_msg(sender="agent-a", channel="general"))

        files = os.listdir(directory)
        assert len(files) == 1
        assert files[0].startswith("mansio_")
        assert files[0].endswith(".md")
        assert "agent-a" in files[0]  # hyphens preserved by _safe_filename
        assert "general" in files[0]

    def test_markdown_format(self, tmp_path):
        directory = str(tmp_path / "mansio")
        injector = OpenClawInjector(directory)
        msg = _make_msg(
            sender="bot-1",
            channel="dev",
            payload="build passed",
            timestamp="2026-08-26T20:00:00Z",
        )

        injector.inject(msg)

        files = os.listdir(directory)
        with open(os.path.join(directory, files[0])) as f:
            content = f.read()

        assert "# Mansio message from bot-1" in content
        assert "**Channel:** dev" in content
        assert "**Type:** chat" in content
        assert "**Time:** 2026-08-26T20:00:00Z" in content
        assert "build passed" in content

    def test_markdown_includes_reply_to(self, tmp_path):
        directory = str(tmp_path / "mansio")
        injector = OpenClawInjector(directory)
        msg = _make_msg(parent_id="parent-id-123")

        injector.inject(msg)

        files = os.listdir(directory)
        with open(os.path.join(directory, files[0])) as f:
            content = f.read()
        assert "**Reply to:** parent-id-123" in content

    def test_multiple_messages_separate_files(self, tmp_path):
        directory = str(tmp_path / "mansio")
        injector = OpenClawInjector(directory)

        injector.inject(_make_msg(payload="first"))
        injector.inject(_make_msg(payload="second"))

        files = os.listdir(directory)
        assert len(files) == 2

    def test_creates_directory(self, tmp_path):
        directory = str(tmp_path / "deep" / "nested" / "mansio")
        injector = OpenClawInjector(directory)

        injector.inject(_make_msg())

        assert os.path.isdir(directory)

    def test_directory_property(self, tmp_path):
        directory = str(tmp_path / "mansio")
        injector = OpenClawInjector(directory)
        assert injector.directory == directory

    def test_close_is_noop(self, tmp_path):
        injector = OpenClawInjector(str(tmp_path / "mansio"))
        injector.close()  # should not raise

    def test_special_chars_in_channel(self, tmp_path):
        directory = str(tmp_path / "mansio")
        injector = OpenClawInjector(directory)

        injector.inject(_make_msg(channel="dm:agent-a:agent-b"))

        files = os.listdir(directory)
        assert len(files) == 1
        # Colons replaced with underscores
        assert "dm_agent-a_agent-b" in files[0]


# ── WebhookInjector ───────────────────────────────────────────────


class TestWebhookInjector:
    def test_inject_posts_json(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                body = self.rfile.read(length)
                received.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass  # suppress logs

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()

        try:
            injector = WebhookInjector(
                f"http://127.0.0.1:{port}/hook",
                timeout=5.0,
            )
            injector.inject(_make_msg(payload="webhook test"))
        finally:
            server.server_close()
            thread.join(timeout=3)

        assert len(received) == 1
        assert received[0]["payload"] == "webhook test"
        assert received[0]["sender"] == "agent-a"

    def test_inject_includes_custom_headers(self):
        received_headers = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received_headers["Authorization"] = self.headers.get("Authorization")
                received_headers["X-Custom"] = self.headers.get("X-Custom")
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()

        try:
            injector = WebhookInjector(
                f"http://127.0.0.1:{port}/hook",
                headers={
                    "Authorization": "Bearer secret",
                    "X-Custom": "value",
                },
                timeout=5.0,
            )
            injector.inject(_make_msg())
        finally:
            server.server_close()
            thread.join(timeout=3)

        assert received_headers["Authorization"] == "Bearer secret"
        assert received_headers["X-Custom"] == "value"

    def test_inject_failure_is_silent(self):
        """WebhookInjector should not raise on connection failure."""
        injector = WebhookInjector(
            "http://127.0.0.1:1/nonexistent",
            timeout=1.0,
        )
        # Should not raise
        injector.inject(_make_msg())

    def test_url_property(self):
        injector = WebhookInjector("http://example.com/hook")
        assert injector.url == "http://example.com/hook"

    def test_close_is_noop(self):
        injector = WebhookInjector("http://example.com/hook")
        injector.close()  # should not raise


# ── Helpers ───────────────────────────────────────────────────────


class TestHelpers:
    def test_msg_to_dict_basic(self):
        msg = _make_msg()
        d = _msg_to_dict(msg)
        assert d["id"] == msg.id
        assert d["channel"] == "general"
        assert d["sender"] == "agent-a"
        assert d["msg_type"] == "chat"
        assert d["payload"] == "hello world"
        assert d["timestamp"] == "2026-08-26T20:00:00Z"
        assert "metadata" not in d
        assert "parent_id" not in d
        assert "thread_id" not in d

    def test_msg_to_dict_with_optional_fields(self):
        msg = _make_msg(
            metadata={"key": "val"},
            parent_id="p1",
            thread_id="t1",
        )
        d = _msg_to_dict(msg)
        assert d["metadata"] == {"key": "val"}
        assert d["parent_id"] == "p1"
        assert d["thread_id"] == "t1"

    def test_safe_filename(self):
        assert _safe_filename("agent-a") == "agent-a"
        assert _safe_filename("dm:a:b") == "dm_a_b"
        assert _safe_filename("hello world") == "hello_world"
        assert _safe_filename("_system:agents") == "_system_agents"
        assert _safe_filename("") == ""


# ── MansioClient.listen() ────────────────────────────────────────


class TestClientListen:
    """Test the listen() convenience method on MansioClient."""

    def test_listen_subscribes_all_channels(self, tmp_path):
        """listen() should call subscribe() for each channel."""
        from mansio_client.client import MansioClient

        # Mock the transport to avoid network calls
        with patch.object(MansioClient, "__init__", lambda self, *a, **kw: None):
            client = MansioClient.__new__(MansioClient)
            client._transport = MagicMock()
            client._agent_id = "test"
            client._display_name = "test"
            client._cursors = {}

            # subscribe returns a sub ID
            client._transport.subscribe.side_effect = [
                "sub-1",
                "sub-2",
                "sub-3",
            ]

            injector = MailboxInjector(str(tmp_path / "inbox.jsonl"))
            sub_ids = client.listen(["general", "inbox", "alerts"], injector)

        assert sub_ids == ["sub-1", "sub-2", "sub-3"]
        assert client._transport.subscribe.call_count == 3
        # Verify channels
        channels_called = [call.args[0] for call in client._transport.subscribe.call_args_list]
        assert channels_called == ["general", "inbox", "alerts"]
        # Verify callback is injector.inject
        for call in client._transport.subscribe.call_args_list:
            assert call.args[1] == injector.inject

    def test_listen_empty_channels(self, tmp_path):
        """listen() with empty list should return empty list."""
        from mansio_client.client import MansioClient

        with patch.object(MansioClient, "__init__", lambda self, *a, **kw: None):
            client = MansioClient.__new__(MansioClient)
            client._transport = MagicMock()
            client._agent_id = "test"
            client._display_name = "test"
            client._cursors = {}

            injector = MailboxInjector(str(tmp_path / "inbox.jsonl"))
            sub_ids = client.listen([], injector)

        assert sub_ids == []
        assert client._transport.subscribe.call_count == 0
