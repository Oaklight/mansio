"""Tests for the MCP server module."""

from __future__ import annotations

import json
import unittest
from io import StringIO
from typing import Any
from unittest.mock import MagicMock, patch

from mansio_client.types import AgentPresence, Message

from mansio.mcp import (
    _TOOLS,
    _call_tool,
    _handle_request,
    _msg_to_dict,
    serve,
)


def _make_msg(**overrides: Any) -> Message:
    """Create a Message with sensible defaults."""
    defaults = {
        "id": "msg-1",
        "channel": "general",
        "sender": "agent-a",
        "msg_type": "chat",
        "payload": "hello",
        "timestamp": "2026-01-01T00:00:00Z",
        "metadata": None,
        "parent_id": None,
        "thread_id": None,
    }
    defaults.update(overrides)
    return Message(**defaults)


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
        )
        d = _msg_to_dict(msg)
        assert d["metadata"] == {"key": "val"}
        assert d["parent_id"] == "msg-0"
        assert d["thread_id"] == "msg-0"


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
            parent_id=None,
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
            parent_id="msg-1",
        )

    def test_read(self) -> None:
        self.client.channel_read.return_value = [_make_msg()]
        result = _call_tool(self.client, "mansio_read", {"channel": "general"})
        assert len(result) == 1
        assert result[0]["id"] == "msg-1"
        self.client.channel_read.assert_called_once_with(
            "general",
            limit=10,
            order="newest",
            thread_id=None,
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
            order="oldest",
            thread_id="msg-0",
        )

    def test_poll(self) -> None:
        self.client.channel_poll.return_value = [_make_msg()]
        result = _call_tool(self.client, "mansio_poll", {"channel": "general"})
        assert len(result) == 1

    def test_dm_send(self) -> None:
        self.client.dm_send.return_value = "dm-1"
        result = _call_tool(
            self.client,
            "mansio_dm_send",
            {
                "to_agent": "agent-b",
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
                "with_agent": "agent-b",
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

    def test_agents(self) -> None:
        self.client.agents.return_value = [
            AgentPresence(
                agent_id="agent-a",
                status="online",
                last_seen="2026-01-01T00:00:00Z",
                metadata={"display_name": "Agent A"},
            ),
        ]
        result = _call_tool(self.client, "mansio_agents", {})
        assert len(result) == 1
        assert result[0]["agent_id"] == "agent-a"
        assert result[0]["metadata"]["display_name"] == "Agent A"

    def test_heartbeat(self) -> None:
        result = _call_tool(self.client, "mansio_heartbeat", {})
        assert result == {"status": "ok"}
        self.client.heartbeat.assert_called_once()

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


class TestServeLoop(unittest.TestCase):
    """Integration test for the stdio serve loop."""

    @patch("mansio.mcp.MansioClient")
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

    @patch("mansio.mcp.MansioClient")
    def test_serve_handles_parse_error(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = MagicMock()

        stdin = StringIO("not json\n")
        stdout = StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            serve("http://localhost:8742", "test-agent")

        output = stdout.getvalue().strip()
        resp = json.loads(output)
        assert resp["error"]["code"] == -32700


if __name__ == "__main__":
    unittest.main()
