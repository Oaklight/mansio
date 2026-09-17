"""MCP (Model Context Protocol) server for mansio.

Implements JSON-RPC 2.0 over stdio, exposing mansio client operations
as MCP tools. Zero external dependencies — uses ``MansioClient`` from
the mansio-client package as the underlying transport.

Usage::

    mansio-mcp --url http://localhost:8742 --user-id my-agent --token mst-xxx

The server reads JSON-RPC requests from stdin and writes responses to
stdout, one JSON object per line.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from mansio_client import MansioClient
from mansio_client.types import ACLEntry, Message

# ── Protocol Constants ────────────────────────────────────────────

_JSONRPC = "2.0"
_MCP_PROTOCOL_VERSION = "2024-11-05"

_SERVER_INFO = {
    "name": "mansio-mcp",
    "version": "0.1.0",
}

_SERVER_CAPABILITIES = {
    "tools": {"listChanged": False},
}


# ── Tool Definitions ──────────────────────────────────────────────

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "mansio_channels",
        "description": "List available channels on the mansio server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "boolean",
                    "description": "If true, return metadata (message_count, last_activity) per channel.",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "mansio_send",
        "description": "Send a message to a channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Target channel name."},
                "content": {"type": "string", "description": "Message content."},
                "msg_type": {
                    "type": "string",
                    "description": "Message type (default: chat).",
                    "default": "chat",
                },
                "parent_id": {
                    "type": "string",
                    "description": "ID of the message being replied to (threading).",
                },
            },
            "required": ["channel", "content"],
        },
    },
    {
        "name": "mansio_read",
        "description": "Read messages from a channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel to read from."},
                "limit": {
                    "type": "integer",
                    "description": "Max messages to return (default: 10).",
                    "default": 10,
                },
                "order": {
                    "type": "string",
                    "enum": ["oldest", "newest"],
                    "description": "Sort order (default: newest).",
                    "default": "newest",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Filter to messages in this thread.",
                },
            },
            "required": ["channel"],
        },
    },
    {
        "name": "mansio_poll",
        "description": "Poll for new messages since last read (cursor-based).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel to poll."},
            },
            "required": ["channel"],
        },
    },
    {
        "name": "mansio_dm_send",
        "description": "Send a direct message to another agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to_user": {"type": "string", "description": "Recipient user ID."},
                "content": {"type": "string", "description": "Message content."},
            },
            "required": ["to_user", "content"],
        },
    },
    {
        "name": "mansio_dm_read",
        "description": "Read direct messages with another agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "with_user": {
                    "type": "string",
                    "description": "The other user's ID.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages to return (default: 10).",
                    "default": 10,
                },
            },
            "required": ["with_user"],
        },
    },
    {
        "name": "mansio_note",
        "description": "Write a note to the agent's private notebook.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Note content."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization.",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "mansio_note_read",
        "description": "Read notes from the agent's private notebook.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by tags.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max notes to return (default: 10).",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "mansio_memory_store",
        "description": "Store a memory for later recall.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Memory content."},
                "memory_type": {
                    "type": "string",
                    "description": "Category (default: general).",
                    "default": "general",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "mansio_memory_recall",
        "description": "Recall stored memories matching a query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "mansio_agents",
        "description": "List agents with recent presence on the server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Consider agents active within this window (default: 120).",
                    "default": 120,
                },
            },
        },
    },
    {
        "name": "mansio_heartbeat",
        "description": "Send a heartbeat to indicate this agent is alive.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "mansio_channel_create",
        "description": "Create a new channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Channel name to create.",
                },
                "visibility": {
                    "type": "string",
                    "description": "Channel visibility (default: public).",
                    "enum": ["public", "private"],
                    "default": "public",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "mansio_channel_delete",
        "description": "Delete a channel and all its messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Channel name to delete.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "mansio_message_delete",
        "description": "Delete a single message by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "ID of the message to delete.",
                },
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "mansio_acl_get",
        "description": "Get ACL entries for a channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel name.",
                },
            },
            "required": ["channel"],
        },
    },
    {
        "name": "mansio_acl_set",
        "description": "Replace all ACL entries for a channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel name.",
                },
                "entries": {
                    "type": "array",
                    "description": "List of ACL entries ({user_id, permission}).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "user_id": {"type": "string"},
                            "permission": {
                                "type": "string",
                                "enum": ["read", "write", "admin"],
                                "default": "read",
                            },
                        },
                        "required": ["user_id"],
                    },
                },
            },
            "required": ["channel", "entries"],
        },
    },
    {
        "name": "mansio_acl_add",
        "description": "Add an ACL entry for a user on a channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel name.",
                },
                "user_id": {
                    "type": "string",
                    "description": "User to grant access.",
                },
                "permission": {
                    "type": "string",
                    "description": "Permission level (default: read).",
                    "enum": ["read", "write", "admin"],
                    "default": "read",
                },
            },
            "required": ["channel", "user_id"],
        },
    },
    {
        "name": "mansio_acl_remove",
        "description": "Remove a user's ACL entry from a channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel name.",
                },
                "user_id": {
                    "type": "string",
                    "description": "User to remove.",
                },
            },
            "required": ["channel", "user_id"],
        },
    },
    {
        "name": "mansio_registry_lookup",
        "description": "Check if a user is registered on the server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "User ID to look up.",
                },
            },
            "required": ["user_id"],
        },
    },
]


# ── Message Serialization ─────────────────────────────────────────


def _msg_to_dict(msg: Message) -> dict[str, Any]:
    """Convert a Message to a plain dict for JSON output."""
    d: dict[str, Any] = {
        "id": msg.id,
        "channel": msg.channel,
        "sender": msg.sender,
        "msg_type": msg.msg_type,
        "payload": msg.payload,
        "timestamp": msg.timestamp,
    }
    if msg.metadata is not None:
        d["metadata"] = msg.metadata
    if msg.parent_id is not None:
        d["parent_id"] = msg.parent_id
    if msg.thread_id is not None:
        d["thread_id"] = msg.thread_id
    return d


# ── Tool Dispatch ─────────────────────────────────────────────────


# ── Tool Handlers ─────────────────────────────────────────────────


def _tool_channels(client: MansioClient, args: dict[str, Any]) -> Any:
    return client.channel_list(detail=args.get("detail", False))


def _tool_send(client: MansioClient, args: dict[str, Any]) -> Any:
    msg_id = client.channel_send(
        args["channel"],
        args["content"],
        msg_type=args.get("msg_type", "chat"),
        parent_id=args.get("parent_id"),
    )
    return {"message_id": msg_id}


def _tool_read(client: MansioClient, args: dict[str, Any]) -> Any:
    msgs = client.channel_read(
        args["channel"],
        limit=args.get("limit", 10),
        order=args.get("order", "newest"),
        thread_id=args.get("thread_id"),
    )
    return [_msg_to_dict(m) for m in msgs]


def _tool_poll(client: MansioClient, args: dict[str, Any]) -> Any:
    return [_msg_to_dict(m) for m in client.channel_poll(args["channel"])]


def _tool_dm_send(client: MansioClient, args: dict[str, Any]) -> Any:
    return {"message_id": client.dm_send(args["to_user"], args["content"])}


def _tool_dm_read(client: MansioClient, args: dict[str, Any]) -> Any:
    msgs = client.dm_read(args["with_user"], limit=args.get("limit", 10))
    return [_msg_to_dict(m) for m in msgs]


def _tool_note(client: MansioClient, args: dict[str, Any]) -> Any:
    return {"message_id": client.note_write(args["content"], tags=args.get("tags"))}


def _tool_note_read(client: MansioClient, args: dict[str, Any]) -> Any:
    msgs = client.note_read(tags=args.get("tags"), limit=args.get("limit", 10))
    return [_msg_to_dict(m) for m in msgs]


def _tool_memory_store(client: MansioClient, args: dict[str, Any]) -> Any:
    msg_id = client.memory_store(
        args["content"], memory_type=args.get("memory_type", "general")
    )
    return {"message_id": msg_id}


def _tool_memory_recall(client: MansioClient, args: dict[str, Any]) -> Any:
    msgs = client.memory_recall(args["query"], limit=args.get("limit", 5))
    return [_msg_to_dict(m) for m in msgs]


def _tool_agents(client: MansioClient, args: dict[str, Any]) -> Any:
    agents = client.users(timeout_seconds=args.get("timeout_seconds", 120))
    return [
        {
            "user_id": a.user_id,
            "status": a.status,
            "last_seen": a.last_seen,
            "metadata": a.metadata,
        }
        for a in agents
    ]


def _tool_heartbeat(client: MansioClient, args: dict[str, Any]) -> Any:
    client.heartbeat()
    return {"status": "ok"}


def _acl_to_dict(entry: ACLEntry) -> dict[str, Any]:
    d: dict[str, Any] = {
        "channel": entry.channel,
        "user_id": entry.user_id,
        "permission": entry.permission,
    }
    if entry.granted_at:
        d["granted_at"] = entry.granted_at
    if entry.granted_by is not None:
        d["granted_by"] = entry.granted_by
    return d


def _tool_channel_create(client: MansioClient, args: dict[str, Any]) -> Any:
    result = client.channel_create(
        args["name"], visibility=args.get("visibility", "public")
    )
    return result


def _tool_channel_delete(client: MansioClient, args: dict[str, Any]) -> Any:
    count = client.channel_delete(args["name"])
    return {"deleted": count}


def _tool_message_delete(client: MansioClient, args: dict[str, Any]) -> Any:
    client.message_delete(args["message_id"])
    return {"status": "ok"}


def _tool_acl_get(client: MansioClient, args: dict[str, Any]) -> Any:
    entries = client.acl_get(args["channel"])
    return [_acl_to_dict(e) for e in entries]


def _tool_acl_set(client: MansioClient, args: dict[str, Any]) -> Any:
    entries = [
        ACLEntry(
            channel=args["channel"],
            user_id=e["user_id"],
            permission=e.get("permission", "read"),
        )
        for e in args["entries"]
    ]
    count = client.acl_set(args["channel"], entries)
    return {"status": "ok", "count": count}


def _tool_acl_add(client: MansioClient, args: dict[str, Any]) -> Any:
    entry = client.acl_add(
        args["channel"], args["user_id"], args.get("permission", "read")
    )
    return _acl_to_dict(entry)


def _tool_acl_remove(client: MansioClient, args: dict[str, Any]) -> Any:
    client.acl_remove(args["channel"], args["user_id"])
    return {"status": "ok"}


def _tool_registry_lookup(client: MansioClient, args: dict[str, Any]) -> Any:
    found = client.registry_lookup(args["user_id"])
    return {"user_id": args["user_id"], "found": found}


_DISPATCH: dict[str, Callable[[MansioClient, dict[str, Any]], Any]] = {
    "mansio_channels": _tool_channels,
    "mansio_send": _tool_send,
    "mansio_read": _tool_read,
    "mansio_poll": _tool_poll,
    "mansio_dm_send": _tool_dm_send,
    "mansio_dm_read": _tool_dm_read,
    "mansio_note": _tool_note,
    "mansio_note_read": _tool_note_read,
    "mansio_memory_store": _tool_memory_store,
    "mansio_memory_recall": _tool_memory_recall,
    "mansio_agents": _tool_agents,
    "mansio_heartbeat": _tool_heartbeat,
    "mansio_channel_create": _tool_channel_create,
    "mansio_channel_delete": _tool_channel_delete,
    "mansio_message_delete": _tool_message_delete,
    "mansio_acl_get": _tool_acl_get,
    "mansio_acl_set": _tool_acl_set,
    "mansio_acl_add": _tool_acl_add,
    "mansio_acl_remove": _tool_acl_remove,
    "mansio_registry_lookup": _tool_registry_lookup,
}


def _call_tool(client: MansioClient, name: str, args: dict[str, Any]) -> Any:
    """Execute a tool and return its result.

    Args:
        client: Active MansioClient instance.
        name: Tool name.
        args: Tool arguments.

    Returns:
        Tool result (serializable).

    Raises:
        ValueError: If tool name is unknown.
    """
    handler = _DISPATCH.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    return handler(client, args)


# ── JSON-RPC Helpers ──────────────────────────────────────────────


def _ok(req_id: Any, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": _JSONRPC, "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC error response."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": _JSONRPC, "id": req_id, "error": err}


# Standard JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


# ── Request Handling ──────────────────────────────────────────────


def _handle_request(client: MansioClient, req: dict[str, Any]) -> dict[str, Any] | None:
    """Process a single JSON-RPC request.

    Args:
        client: Active MansioClient.
        req: Parsed JSON-RPC request dict.

    Returns:
        Response dict, or None for notifications.
    """
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    if method == "initialize":
        return _ok(
            req_id,
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": _SERVER_CAPABILITIES,
                "serverInfo": _SERVER_INFO,
            },
        )

    if method == "notifications/initialized":
        # Client acknowledgment — no response needed
        return None

    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        return _ok(req_id, {"tools": _TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        try:
            result = _call_tool(client, tool_name, tool_args)
            return _ok(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False),
                        },
                    ],
                },
            )
        except ValueError as exc:
            return _error(req_id, _INVALID_PARAMS, str(exc))
        except Exception as exc:
            return _ok(
                req_id,
                {
                    "content": [
                        {"type": "text", "text": str(exc)},
                    ],
                    "isError": True,
                },
            )

    return _error(req_id, _METHOD_NOT_FOUND, f"Unknown method: {method}")


# ── Main Loop ─────────────────────────────────────────────────────


def serve(
    url: str,
    user_id: str,
    *,
    token: str | None = None,
    display_name: str | None = None,
) -> None:
    """Run the MCP server, reading from stdin and writing to stdout.

    Args:
        url: Mansio server URL.
        user_id: Agent ID to connect as.
        token: Optional API token.
        display_name: Optional display name.
    """
    client = MansioClient(url, user_id, token=token, display_name=display_name)

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                resp = _error(None, _PARSE_ERROR, f"Parse error: {exc}")
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
                continue

            # TODO: JSON-RPC batch requests (arrays) are not yet supported.
            # Most MCP clients send one request at a time, so this is fine for v1.
            if not isinstance(req, dict):
                resp = _error(None, _INVALID_REQUEST, "Request must be a JSON object")
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
                continue

            resp = _handle_request(client, req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
    finally:
        client.close()


def main() -> None:
    """CLI entry point for mansio-mcp."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Mansio MCP server (stdin/stdout JSON-RPC)"
    )
    parser.add_argument("--url", required=True, help="Mansio server URL")
    parser.add_argument("--user-id", required=True, help="Agent user ID")
    parser.add_argument("--token", default=None, help="API token")
    parser.add_argument("--display-name", default=None, help="Display name")
    args = parser.parse_args()

    serve(args.url, args.user_id, token=args.token, display_name=args.display_name)
