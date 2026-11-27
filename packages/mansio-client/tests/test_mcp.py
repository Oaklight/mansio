"""Tests for the MCP server module."""

from __future__ import annotations

import inspect
import json
import os
import unittest
from io import StringIO
from typing import Any
from unittest.mock import MagicMock, patch

from mansio_client import MansioAPIError, MansioClient
from mansio_client.mcp import (
    _DISPATCH,
    _TOOLS,
    _call_tool,
    _handle_request,
    _msg_to_dict,
    main,
    serve,
)
from mansio_core.types import (
    ALLOWED_PERMISSIONS,
    ACLEntry,
    ChannelMeta,
    ClaimResult,
    Message,
    UserPresence,
)


def _make_msg(**overrides: Any) -> Message:
    """Create a Message with sensible defaults."""
    defaults = {
        "id": "msg-1",
        "channel": "general",
        "sender": "user-a",
        "msg_type": "chat",
        "payload": "hello",
        "timestamp": "2026-01-01T00:00:00Z",
        "metadata": None,
        "parent_id": None,
        "thread_id": None,
        "intent": None,
    }
    defaults.update(overrides)
    return Message(**defaults)


def _make_acl_entry(**overrides: Any) -> ACLEntry:
    """Create an ACLEntry with sensible defaults."""
    defaults = {
        "channel": "general",
        "user_id": "user-a",
        "permission": "write",
        "granted_at": "2026-01-01T00:00:00Z",
        "granted_by": None,
    }
    defaults.update(overrides)
    return ACLEntry(**defaults)


class TestMsgToDict(unittest.TestCase):
    def test_basic(self) -> None:
        msg = _make_msg()
        d = _msg_to_dict(msg)
        assert d["id"] == "msg-1"
        assert d["payload"] == "hello"
        assert "metadata" not in d
        assert "parent_id" not in d

    def test_with_metadata_and_threading(self) -> None:
        msg = _make_msg(
            metadata={"key": "val"},
            parent_id="msg-0",
            thread_id="msg-0",
            intent="DIRECT_QUESTION",
        )
        d = _msg_to_dict(msg)
        assert d["metadata"] == {"key": "val"}
        assert d["parent_id"] == "msg-0"
        assert d["thread_id"] == "msg-0"
        assert d["intent"] == "DIRECT_QUESTION"


class TestHandleRequest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock()

    def test_initialize(self) -> None:
        resp = _handle_request(
            self.client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            },
        )
        assert resp is not None
        assert resp["id"] == 1
        result = resp["result"]
        assert "protocolVersion" in result
        assert "capabilities" in result
        assert "serverInfo" in result

    def test_initialized_notification(self) -> None:
        resp = _handle_request(
            self.client,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        assert resp is None

    def test_ping(self) -> None:
        resp = _handle_request(
            self.client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "ping",
            },
        )
        assert resp is not None
        assert resp["result"] == {}

    def test_tools_list(self) -> None:
        resp = _handle_request(
            self.client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/list",
                "params": {},
            },
        )
        assert resp is not None
        tools = resp["result"]["tools"]
        assert len(tools) == len(_TOOLS)
        names = {t["name"] for t in tools}
        assert "mansio_send" in names
        assert "mansio_channels" in names

    def test_unknown_method(self) -> None:
        resp = _handle_request(
            self.client,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "foo/bar",
            },
        )
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_tools_call_success(self) -> None:
        self.client.channel_list.return_value = ["general", "random"]
        resp = _handle_request(
            self.client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "mansio_channels", "arguments": {}},
            },
        )
        assert resp is not None
        assert "result" in resp
        content = resp["result"]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        data = json.loads(content[0]["text"])
        assert data == ["general", "random"]

    def test_tools_call_unknown_tool(self) -> None:
        resp = _handle_request(
            self.client,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "nonexistent", "arguments": {}},
            },
        )
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_tools_call_runtime_error(self) -> None:
        self.client.channel_list.side_effect = ConnectionError("server down")
        resp = _handle_request(
            self.client,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "mansio_channels", "arguments": {}},
            },
        )
        assert resp is not None
        result = resp["result"]
        assert result["isError"] is True
        assert "server down" in result["content"][0]["text"]


class TestCallTool(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock()

    def test_channels(self) -> None:
        self.client.channel_list.return_value = ["a", "b"]
        result = _call_tool(self.client, "mansio_channels", {})
        assert result == ["a", "b"]
        self.client.channel_list.assert_called_once_with(detail=False)

    def test_channels_detail(self) -> None:
        self.client.channel_list.return_value = [{"name": "a", "message_count": 5}]
        _result = _call_tool(self.client, "mansio_channels", {"detail": True})
        self.client.channel_list.assert_called_once_with(detail=True)

    def test_send(self) -> None:
        self.client.channel_send.return_value = "msg-42"
        result = _call_tool(
            self.client,
            "mansio_send",
            {
                "channel": "general",
                "content": "hello",
            },
        )
        assert result == {"message_id": "msg-42"}
        self.client.channel_send.assert_called_once_with(
            "general",
            "hello",
            msg_type="chat",
            metadata=None,
            parent_id=None,
            intent=None,
        )

    def test_send_with_metadata(self) -> None:
        self.client.channel_send.return_value = "msg-45"
        _call_tool(
            self.client,
            "mansio_send",
            {
                "channel": "general",
                "content": "hello",
                "metadata": {"correlation_id": "c-1", "priority": "high"},
            },
        )
        self.client.channel_send.assert_called_once_with(
            "general",
            "hello",
            msg_type="chat",
            metadata={"correlation_id": "c-1", "priority": "high"},
            parent_id=None,
            intent=None,
        )

    def test_send_with_parent(self) -> None:
        self.client.channel_send.return_value = "msg-43"
        _result = _call_tool(
            self.client,
            "mansio_send",
            {
                "channel": "general",
                "content": "reply",
                "parent_id": "msg-1",
                "msg_type": "note",
            },
        )
        self.client.channel_send.assert_called_once_with(
            "general",
            "reply",
            msg_type="note",
            metadata=None,
            parent_id="msg-1",
            intent=None,
        )

    def test_send_with_intent(self) -> None:
        self.client.channel_send.return_value = "msg-44"
        _result = _call_tool(
            self.client,
            "mansio_send",
            {
                "channel": "general",
                "content": "ping",
                "intent": "REQUIRES_RESPONSE",
            },
        )
        self.client.channel_send.assert_called_once_with(
            "general",
            "ping",
            msg_type="chat",
            metadata=None,
            parent_id=None,
            intent="REQUIRES_RESPONSE",
        )

    def test_read(self) -> None:
        self.client.channel_read.return_value = [_make_msg()]
        result = _call_tool(self.client, "mansio_read", {"channel": "general"})
        assert len(result) == 1
        assert result[0]["id"] == "msg-1"
        self.client.channel_read.assert_called_once_with(
            "general",
            limit=10,
            after=None,
            order="newest",
            thread_id=None,
            intent=None,
            include_own=False,
        )

    def test_read_after_cursor(self) -> None:
        self.client.channel_read.return_value = []
        _call_tool(
            self.client,
            "mansio_read",
            {"channel": "general", "after": "msg-100"},
        )
        self.client.channel_read.assert_called_once_with(
            "general",
            limit=10,
            after="msg-100",
            order="newest",
            thread_id=None,
            intent=None,
            include_own=False,
        )

    def test_read_with_thread(self) -> None:
        self.client.channel_read.return_value = []
        _call_tool(
            self.client,
            "mansio_read",
            {
                "channel": "dev",
                "thread_id": "msg-0",
                "limit": 5,
                "order": "oldest",
            },
        )
        self.client.channel_read.assert_called_once_with(
            "dev",
            limit=5,
            after=None,
            order="oldest",
            thread_id="msg-0",
            intent=None,
            include_own=False,
        )

    def test_read_with_intent(self) -> None:
        self.client.channel_read.return_value = []
        _call_tool(
            self.client,
            "mansio_read",
            {
                "channel": "dev",
                "intent": "FYI_ONLY",
            },
        )
        self.client.channel_read.assert_called_once_with(
            "dev",
            limit=10,
            after=None,
            order="newest",
            thread_id=None,
            intent="FYI_ONLY",
            include_own=False,
        )

    def test_read_with_include_own(self) -> None:
        self.client.channel_read.return_value = []
        _call_tool(
            self.client,
            "mansio_read",
            {"channel": "dev", "include_own": True},
        )
        self.client.channel_read.assert_called_once_with(
            "dev",
            limit=10,
            after=None,
            order="newest",
            thread_id=None,
            intent=None,
            include_own=True,
        )

    def test_poll(self) -> None:
        self.client.channel_poll.return_value = [_make_msg()]
        result = _call_tool(self.client, "mansio_poll", {"channel": "general"})
        assert len(result) == 1
        self.client.channel_poll.assert_called_once_with("general", include_own=False)

    def test_poll_with_include_own(self) -> None:
        self.client.channel_poll.return_value = []
        _call_tool(
            self.client,
            "mansio_poll",
            {"channel": "general", "include_own": True},
        )
        self.client.channel_poll.assert_called_once_with("general", include_own=True)

    def test_queue_publish(self) -> None:
        self.client.queue_publish.return_value = "msg-q1"
        result = _call_tool(
            self.client,
            "mansio_queue_publish",
            {"channel": "work", "content": "do it"},
        )
        assert result == {"message_id": "msg-q1"}
        self.client.queue_publish.assert_called_once_with(
            "work", "do it", msg_type="task", metadata=None
        )

    def test_queue_publish_with_metadata(self) -> None:
        self.client.queue_publish.return_value = "msg-q1"
        _call_tool(
            self.client,
            "mansio_queue_publish",
            {
                "channel": "work",
                "content": "do it",
                "msg_type": "job",
                "metadata": {"priority": "high"},
            },
        )
        self.client.queue_publish.assert_called_once_with(
            "work", "do it", msg_type="job", metadata={"priority": "high"}
        )

    def test_queue_claim(self) -> None:
        self.client.queue_claim.return_value = ClaimResult(
            message=_make_msg(id="msg-q1", channel="work"),
            status="claimed",
            claimed_by="user-a",
            claimed_at="2026-01-01T00:00:00Z",
        )
        result = _call_tool(
            self.client,
            "mansio_queue_claim",
            {"channel": "work", "lease_seconds": 45},
        )
        assert result["status"] == "claimed"
        assert result["message"]["id"] == "msg-q1"
        assert result["claimed_by"] == "user-a"
        assert result["claimed_at"] == "2026-01-01T00:00:00Z"
        assert "lease_until" not in result
        self.client.queue_claim.assert_called_once_with("work", lease_seconds=45)

    def test_queue_claim_empty(self) -> None:
        self.client.queue_claim.return_value = None
        result = _call_tool(self.client, "mansio_queue_claim", {"channel": "work"})
        assert result is None
        self.client.queue_claim.assert_called_once_with("work", lease_seconds=300)

    def test_queue_ack(self) -> None:
        self.client.queue_ack.return_value = ClaimResult(
            message=_make_msg(id="msg-q1", channel="work"),
            status="completed",
            claimed_by="user-a",
            claimed_at="2026-01-01T00:00:00Z",
        )
        result = _call_tool(self.client, "mansio_queue_ack", {"message_id": "msg-q1"})
        assert result["status"] == "completed"
        assert result["message"]["id"] == "msg-q1"
        self.client.queue_ack.assert_called_once_with("msg-q1")

    def test_queue_status(self) -> None:
        self.client.queue_status.return_value = {
            "status": "claimed",
            "claimed_by": "user-a",
        }
        result = _call_tool(self.client, "mansio_queue_status", {"message_id": "msg-q1"})
        assert result == {"status": "claimed", "claimed_by": "user-a"}
        self.client.queue_status.assert_called_once_with("msg-q1")

    def test_dm_send(self) -> None:
        self.client.dm_send.return_value = "dm-1"
        result = _call_tool(
            self.client,
            "mansio_dm_send",
            {
                "to_user": "user-b",
                "content": "hi",
            },
        )
        assert result == {"message_id": "dm-1"}

    def test_dm_read(self) -> None:
        self.client.dm_read.return_value = [_make_msg()]
        result = _call_tool(
            self.client,
            "mansio_dm_read",
            {
                "with_user": "user-b",
            },
        )
        assert len(result) == 1

    def test_note(self) -> None:
        self.client.note_write.return_value = "note-1"
        result = _call_tool(
            self.client,
            "mansio_note",
            {
                "content": "remember this",
                "tags": ["todo"],
            },
        )
        assert result == {"message_id": "note-1"}
        self.client.note_write.assert_called_once_with("remember this", tags=["todo"])

    def test_note_read(self) -> None:
        self.client.note_read.return_value = [_make_msg(msg_type="note")]
        result = _call_tool(self.client, "mansio_note_read", {"tags": ["todo"]})
        assert len(result) == 1

    def test_memory_store(self) -> None:
        self.client.memory_store.return_value = "mem-1"
        result = _call_tool(
            self.client,
            "mansio_memory_store",
            {
                "content": "important fact",
            },
        )
        assert result == {"message_id": "mem-1"}

    def test_memory_recall(self) -> None:
        self.client.memory_recall.return_value = [_make_msg(msg_type="memory")]
        result = _call_tool(self.client, "mansio_memory_recall", {"query": "fact"})
        assert len(result) == 1

    def test_thought(self) -> None:
        self.client.thought_record.return_value = "thought-1"
        result = _call_tool(
            self.client,
            "mansio_thought",
            {
                "content": "pondering",
                "thinking_mode": "reasoning",
                "focus_area": "design",
            },
        )
        assert result == {"message_id": "thought-1"}
        self.client.thought_record.assert_called_once_with(
            "pondering", thinking_mode="reasoning", focus_area="design"
        )

    def test_thought_read(self) -> None:
        self.client.thought_read.return_value = [_make_msg(msg_type="thought")]
        result = _call_tool(self.client, "mansio_thought_read", {"limit": 3})
        assert len(result) == 1
        self.client.thought_read.assert_called_once_with(limit=3)

    def test_broadcast_list(self) -> None:
        self.client.broadcast_list.return_value = ["news", "releases"]
        result = _call_tool(self.client, "mansio_broadcast_list", {})
        assert result == ["news", "releases"]
        self.client.broadcast_list.assert_called_once_with()

    def test_broadcast_read(self) -> None:
        self.client.broadcast_read.return_value = [_make_msg()]
        result = _call_tool(self.client, "mansio_broadcast_read", {"topic": "news", "limit": 5})
        assert len(result) == 1
        self.client.broadcast_read.assert_called_once_with("news", limit=5)

    def test_check_unread(self) -> None:
        self.client.check_unread.return_value = {"general": 2, "dev": 1}
        result = _call_tool(self.client, "mansio_check_unread", {})
        assert result == {"unread": {"general": 2, "dev": 1}, "total_unread": 3}
        self.client.check_unread.assert_called_once_with(include_own=False)

    def test_check_unread_include_own(self) -> None:
        self.client.check_unread.return_value = {"general": 1}
        result = _call_tool(self.client, "mansio_check_unread", {"include_own": True})
        assert result == {"unread": {"general": 1}, "total_unread": 1}
        self.client.check_unread.assert_called_once_with(include_own=True)

    def test_notifications(self) -> None:
        self.client.notification_check.return_value = [_make_msg()]
        result = _call_tool(self.client, "mansio_notifications", {})
        assert len(result) == 1
        self.client.notification_check.assert_called_once_with()

    def test_user_status(self) -> None:
        self.client.user_status.return_value = UserPresence(
            user_id="user-a",
            status="online",
            last_seen="2026-01-01T00:00:00Z",
            metadata={"display_name": "Agent A"},
        )
        result = _call_tool(self.client, "mansio_user_status", {"user_id": "user-a"})
        assert result["user_id"] == "user-a"
        assert result["status"] == "online"
        assert result["metadata"]["display_name"] == "Agent A"
        self.client.user_status.assert_called_once_with("user-a", timeout_seconds=120)

    def test_user_status_unknown(self) -> None:
        self.client.user_status.return_value = None
        result = _call_tool(self.client, "mansio_user_status", {"user_id": "ghost"})
        assert result is None

    def test_agents(self) -> None:
        self.client.users.return_value = [
            UserPresence(
                user_id="user-a",
                status="online",
                last_seen="2026-01-01T00:00:00Z",
                metadata={"display_name": "Agent A"},
            ),
        ]
        result = _call_tool(self.client, "mansio_agents", {})
        assert len(result) == 1
        assert result[0]["user_id"] == "user-a"
        assert result[0]["metadata"]["display_name"] == "Agent A"

    def test_heartbeat(self) -> None:
        result = _call_tool(self.client, "mansio_heartbeat", {})
        assert result == {"status": "ok"}
        self.client.heartbeat.assert_called_once_with(metadata=None)

    def test_heartbeat_with_metadata(self) -> None:
        result = _call_tool(self.client, "mansio_heartbeat", {"metadata": {"status": "busy"}})
        assert result == {"status": "ok"}
        self.client.heartbeat.assert_called_once_with(metadata={"status": "busy"})

    def test_channel_create(self) -> None:
        self.client.channel_create.return_value = {
            "name": "test-ch",
            "owner": "user-a",
            "visibility": "public",
            "created_at": "2026-01-01T00:00:00Z",
        }
        result = _call_tool(self.client, "mansio_channel_create", {"name": "test-ch"})
        assert result["name"] == "test-ch"
        self.client.channel_create.assert_called_once_with("test-ch", visibility="public")

    def test_channel_create_private(self) -> None:
        self.client.channel_create.return_value = {
            "name": "secret",
            "owner": "user-a",
            "visibility": "private",
            "created_at": "2026-01-01T00:00:00Z",
        }
        result = _call_tool(
            self.client,
            "mansio_channel_create",
            {"name": "secret", "visibility": "private"},
        )
        assert result["visibility"] == "private"
        self.client.channel_create.assert_called_once_with("secret", visibility="private")

    def test_channel_delete(self) -> None:
        self.client.channel_delete.return_value = 5
        result = _call_tool(self.client, "mansio_channel_delete", {"name": "old-ch"})
        assert result == {"deleted": 5}
        self.client.channel_delete.assert_called_once_with("old-ch")

    def test_channel_meta(self) -> None:
        self.client.channel_meta.return_value = ChannelMeta(
            name="ops",
            owner="alice",
            visibility="private",
            created_at="2026-01-01T00:00:00Z",
        )
        result = _call_tool(self.client, "mansio_channel_meta", {"channel": "ops"})
        assert result == {
            "name": "ops",
            "owner": "alice",
            "visibility": "private",
            "created_at": "2026-01-01T00:00:00Z",
        }
        self.client.channel_meta.assert_called_once_with("ops")

    def test_channel_meta_404_surfaces_as_api_error(self) -> None:
        self.client.channel_meta.side_effect = MansioAPIError(404, "Channel 'ops' not found")
        resp = _handle_request(
            self.client,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "mansio_channel_meta",
                    "arguments": {"channel": "ops"},
                },
            },
        )
        assert resp is not None
        assert resp["result"]["isError"] is True
        assert "not found" in resp["result"]["content"][0]["text"]
        self.client.channel_meta.assert_called_once_with("ops")

    def test_message_delete(self) -> None:
        self.client.message_delete.return_value = True
        result = _call_tool(self.client, "mansio_message_delete", {"message_id": "msg-99"})
        assert result == {"status": "ok"}
        self.client.message_delete.assert_called_once_with("msg-99")

    def test_acl_get(self) -> None:
        self.client.acl_get.return_value = [
            _make_acl_entry(user_id="user-a", permission="admin"),
            _make_acl_entry(user_id="user-b", permission="read"),
        ]
        result = _call_tool(self.client, "mansio_acl_get", {"channel": "general"})
        assert len(result) == 2
        assert result[0]["user_id"] == "user-a"
        assert result[0]["permission"] == "admin"
        self.client.acl_get.assert_called_once_with("general")

    def test_acl_set(self) -> None:
        self.client.acl_set.return_value = 2
        result = _call_tool(
            self.client,
            "mansio_acl_set",
            {
                "channel": "general",
                "entries": [
                    {"user_id": "user-a", "permission": "admin"},
                    {"user_id": "user-b"},
                ],
            },
        )
        assert result == {"status": "ok", "count": 2}

    def test_acl_add(self) -> None:
        self.client.acl_add.return_value = _make_acl_entry(user_id="user-b", permission="write")
        result = _call_tool(
            self.client,
            "mansio_acl_add",
            {"channel": "general", "user_id": "user-b", "permission": "write"},
        )
        assert result["user_id"] == "user-b"
        assert result["permission"] == "write"
        self.client.acl_add.assert_called_once_with("general", "user-b", "write")

    def test_acl_remove(self) -> None:
        self.client.acl_remove.return_value = True
        result = _call_tool(
            self.client,
            "mansio_acl_remove",
            {"channel": "general", "user_id": "user-b"},
        )
        assert result == {"status": "ok"}
        self.client.acl_remove.assert_called_once_with("general", "user-b")

    def test_registry_lookup(self) -> None:
        self.client.registry_lookup.return_value = True
        result = _call_tool(self.client, "mansio_registry_lookup", {"user_id": "user-a"})
        assert result == {"user_id": "user-a", "found": True}
        self.client.registry_lookup.assert_called_once_with("user-a")

    def test_registry_lookup_not_found(self) -> None:
        self.client.registry_lookup.return_value = False
        result = _call_tool(self.client, "mansio_registry_lookup", {"user_id": "ghost"})
        assert result == {"user_id": "ghost", "found": False}

    def test_unknown_tool(self) -> None:
        with self.assertRaises(ValueError, msg="Unknown tool: bogus"):
            _call_tool(self.client, "bogus", {})


class TestToolDefinitions(unittest.TestCase):
    """Validate tool schema structure."""

    def test_all_tools_have_required_fields(self) -> None:
        for tool in _TOOLS:
            assert "name" in tool, f"Tool missing name: {tool}"
            assert "description" in tool, f"Tool {tool['name']} missing description"
            assert "inputSchema" in tool, f"Tool {tool['name']} missing inputSchema"
            schema = tool["inputSchema"]
            assert schema.get("type") == "object", (
                f"Tool {tool['name']} inputSchema type must be 'object'"
            )
            assert "properties" in schema, f"Tool {tool['name']} missing properties"

    def test_unique_names(self) -> None:
        names = [t["name"] for t in _TOOLS]
        assert len(names) == len(set(names)), f"Duplicate tool names: {names}"

    def test_observation_tools_present(self) -> None:
        names = {t["name"] for t in _TOOLS}
        expected = {
            "mansio_thought",
            "mansio_thought_read",
            "mansio_broadcast_list",
            "mansio_broadcast_read",
            "mansio_check_unread",
            "mansio_notifications",
            "mansio_user_status",
        }
        assert expected <= names

    def test_send_schema_has_optional_metadata(self) -> None:
        tool = next(t for t in _TOOLS if t["name"] == "mansio_send")
        schema = tool["inputSchema"]
        assert schema["properties"]["metadata"]["type"] == "object"
        assert "metadata" not in schema.get("required", [])

    def test_read_schema_has_optional_after(self) -> None:
        tool = next(t for t in _TOOLS if t["name"] == "mansio_read")
        schema = tool["inputSchema"]
        assert schema["properties"]["after"]["type"] == "string"
        assert "after" not in schema.get("required", [])

    def test_channel_meta_schema_requires_channel(self) -> None:
        tool = next(t for t in _TOOLS if t["name"] == "mansio_channel_meta")
        schema = tool["inputSchema"]
        assert list(schema["properties"]) == ["channel"]
        assert schema["required"] == ["channel"]

    def test_heartbeat_schema_has_optional_metadata(self) -> None:
        tool = next(t for t in _TOOLS if t["name"] == "mansio_heartbeat")
        schema = tool["inputSchema"]
        assert schema["properties"]["metadata"]["type"] == "object"
        assert "required" not in schema

    def test_check_unread_schema_has_optional_include_own(self) -> None:
        tool = next(t for t in _TOOLS if t["name"] == "mansio_check_unread")
        schema = tool["inputSchema"]
        prop = schema["properties"]["include_own"]
        assert prop["type"] == "boolean"
        assert prop["default"] is False
        assert "include_own" not in schema.get("required", [])

    def test_every_tool_is_dispatchable(self) -> None:
        assert {t["name"] for t in _TOOLS} == set(_DISPATCH)


class TestAclPermissionVocabulary(unittest.TestCase):
    """MCP ACL schemas use the shared verb/preset vocabulary."""

    @staticmethod
    def _tool(name: str) -> dict[str, Any]:
        return next(t for t in _TOOLS if t["name"] == name)

    def test_acl_add_enum_matches_shared_set(self) -> None:
        enum = self._tool("mansio_acl_add")["inputSchema"]["properties"]["permission"]["enum"]
        assert enum == sorted(ALLOWED_PERMISSIONS)

    def test_acl_set_enum_matches_shared_set(self) -> None:
        enum = self._tool("mansio_acl_set")["inputSchema"]["properties"]["entries"]["items"][
            "properties"
        ]["permission"]["enum"]
        assert enum == sorted(ALLOWED_PERMISSIONS)

    def test_descriptions_name_presets(self) -> None:
        for name in ("mansio_acl_add", "mansio_acl_set"):
            description = self._tool(name)["description"]
            assert "viewer/member/owner" in description

    def test_bogus_value_surfaces_as_api_error(self) -> None:
        client = MagicMock()
        client.acl_add.side_effect = MansioAPIError(400, "invalid permission")
        resp = _handle_request(
            client,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "mansio_acl_add",
                    "arguments": {
                        "channel": "general",
                        "user_id": "user-a",
                        "permission": "bogus",
                    },
                },
            },
        )
        assert resp is not None
        assert resp["result"]["isError"] is True
        assert "invalid permission" in resp["result"]["content"][0]["text"]


class TestQueueToolSchemas(unittest.TestCase):
    """Queue tool schemas mirror the client method signatures."""

    CASES = [
        ("mansio_queue_publish", "queue_publish"),
        ("mansio_queue_claim", "queue_claim"),
        ("mansio_queue_ack", "queue_ack"),
        ("mansio_queue_status", "queue_status"),
    ]

    def test_queue_tools_are_dispatchable(self) -> None:
        for tool_name, _ in self.CASES:
            assert tool_name in _DISPATCH

    def test_schema_arguments_match_client_method(self) -> None:
        tools = {t["name"]: t for t in _TOOLS}
        for tool_name, method_name in self.CASES:
            parameters = inspect.signature(getattr(MansioClient, method_name)).parameters
            expected = [name for name in parameters if name != "self"]
            props = list(tools[tool_name]["inputSchema"]["properties"])
            assert props == expected, tool_name


class TestServeLoop(unittest.TestCase):
    """Integration test for the stdio serve loop."""

    @patch("mansio_client.mcp.MansioClient")
    def test_serve_processes_lines(self, mock_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        input_lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n",
        ]
        stdin = StringIO("".join(input_lines))
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            serve("http://localhost:8742", "test-agent", token="mst-xxx")

        output = stdout.getvalue().strip().split("\n")
        assert len(output) == 2

        ping_resp = json.loads(output[0])
        assert ping_resp["id"] == 1
        assert ping_resp["result"] == {}

        list_resp = json.loads(output[1])
        assert list_resp["id"] == 2
        assert "tools" in list_resp["result"]

        mock_client.close.assert_called_once()

    @patch("mansio_client.mcp.MansioClient")
    def test_serve_handles_parse_error(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = MagicMock()

        stdin = StringIO("not json\n")
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            serve("http://localhost:8742", "test-agent")

        output = stdout.getvalue().strip()
        resp = json.loads(output)
        assert resp["error"]["code"] == -32700


class TestMain(unittest.TestCase):
    """The mansio-mcp entry point resolves settings from flags and env."""

    @patch("mansio_client.mcp.serve")
    def test_flags(self, mock_serve: MagicMock) -> None:
        argv = [
            "mansio-mcp",
            "--url",
            "http://localhost:8742",
            "--user-id",
            "flag-agent",
            "--token",
            "mst-flag",
        ]
        with patch("sys.argv", argv), patch.dict(os.environ, {}, clear=True):
            main()

        mock_serve.assert_called_once_with(
            "http://localhost:8742",
            "flag-agent",
            token="mst-flag",
            display_name=None,
        )

    @patch("mansio_client.mcp.serve")
    def test_env_fallback(self, mock_serve: MagicMock) -> None:
        env = {
            "MANSIO_URL": "http://localhost:8742",
            "MANSIO_USER_ID": "env-agent",
            "MANSIO_TOKEN": "mst-env",
            "MANSIO_DISPLAY_NAME": "Env Agent",
        }
        with patch("sys.argv", ["mansio-mcp"]), patch.dict(os.environ, env, clear=True):
            main()

        mock_serve.assert_called_once_with(
            "http://localhost:8742",
            "env-agent",
            token="mst-env",
            display_name="Env Agent",
        )

    @patch("mansio_client.mcp.serve")
    def test_flags_override_env(self, mock_serve: MagicMock) -> None:
        env = {"MANSIO_URL": "http://env:8742", "MANSIO_USER_ID": "env-agent"}
        argv = ["mansio-mcp", "--user-id", "flag-agent"]
        with patch("sys.argv", argv), patch.dict(os.environ, env, clear=True):
            main()

        mock_serve.assert_called_once_with(
            "http://env:8742", "flag-agent", token=None, display_name=None
        )

    @patch("mansio_client.mcp.serve")
    def test_legacy_agent_id_env(self, mock_serve: MagicMock) -> None:
        env = {
            "MANSIO_URL": "http://localhost:8742",
            "MANSIO_AGENT_ID": "legacy-agent",
        }
        with (
            patch("sys.argv", ["mansio-mcp"]),
            patch.dict(os.environ, env, clear=True),
            self.assertWarns(DeprecationWarning),
        ):
            main()

        mock_serve.assert_called_once_with(
            "http://localhost:8742", "legacy-agent", token=None, display_name=None
        )

    @patch("mansio_client.mcp.serve")
    def test_missing_required_settings_exits(self, mock_serve: MagicMock) -> None:
        with (
            patch("sys.argv", ["mansio-mcp"]),
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(SystemExit) as ctx,
        ):
            main()

        assert ctx.exception.code == 2
        mock_serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
