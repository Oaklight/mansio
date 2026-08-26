"""HTTP frontend for mansio — REST API + SSE push.

Exposes the Bus over HTTP/HTTPS so remote MansioClient instances can
publish, query, and receive real-time notifications via SSE.

Uses vendored zerodep ``httpserver`` — async, decorator-based routing,
StreamingResponse for SSE.

API Endpoints:
    POST /v1/publish         — publish a message
    GET  /v1/query           — query messages from a channel
    GET  /v1/channels        — list channels
    POST /v1/queue/claim     — claim oldest unclaimed queue message
    POST /v1/queue/ack       — acknowledge a claimed queue message
    GET  /v1/queue/status    — query queue status of a message
    GET  /v1/subscribe       — SSE stream for real-time notifications
    GET  /v1/auth/check      — validate agent credentials
    GET  /v1/registry/lookup — look up agent registration
    GET  /health             — health check

Authentication:
    When a TokenStore is configured, all /v1/* endpoints (except
    /health and /v1/auth/check) require ``Authorization: Bearer mst-...``.
    The token is validated server-side, and the sender field in publish
    requests must match the token's agent_id. Supertokens (agent_id=NULL)
    bypass sender checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import re
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from mansio._vendor.httpserver import App, JSONResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from mansio._vendor.httpserver import Request
    from mansio.bus import Bus
    from mansio.token_store import TokenStore
    from mansio.types import Message

# Paths that skip token auth
_PUBLIC_PATHS = frozenset({"/health", "/v1/auth/check"})

# Channel naming rules:
# - User channels: 3-64 chars, must contain a letter, start with [a-z],
#   end with [a-z0-9], no consecutive special chars
# - System/auto channels (_system:, dm:, notebook:, memory:, broadcast:)
#   are exempt from user rules — validated separately
# User channels: no colons (reserved for system prefixes), Unicode letters OK
# End anchor [^\W_] = letters/digits (no trailing underscore)
_USER_CHANNEL_RE = re.compile(r"^(?=[^\W\d_])[\w.-]{1,63}[^\W_]$")
_NO_CONSECUTIVE_SPECIALS = re.compile(r"[._-]{2}")
_RESERVED_PREFIXES = ("_system:", "dm:", "notebook:", "memory:", "broadcast:")
_SYSTEM_CHANNEL_RE = re.compile(r"^[\w_][\w:.-]{1,126}[\w]$")

# Per-request auth result: str (agent_id), None (supertoken), True (no auth)
_auth_result_var: contextvars.ContextVar[Any] = contextvars.ContextVar("auth_result", default=True)


_PRIVATE_CHANNEL_PREFIXES = ("notebook:", "memory:")


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _is_private_channel(channel: str) -> bool:
    """Return True if *channel* is a DM or private-prefix channel.

    Private channels are:
    - ``dm:*``  — direct messages between specific agents
    - Any channel starting with a ``_PRIVATE_CHANNEL_PREFIXES`` entry
      (currently ``notebook:`` and ``memory:``).

    Everything else (plain user channels, ``_system:``, ``broadcast:``,
    and registered-prefix channels) is considered public.
    """
    if channel.startswith("dm:"):
        return True
    return any(channel.startswith(p) for p in _PRIVATE_CHANNEL_PREFIXES)


def _agent_involved(agent_id: str, channel: str, sender: str) -> bool:
    """Check if an agent is involved in a message (sender or channel member).

    For **public** channels every authenticated agent is considered
    involved, so all messages are visible.

    For **private** channels (DMs, notebook, memory) uses exact segment
    matching on ``:``-delimited channel names to avoid substring false
    positives (e.g., "bob" must not match "bobby").
    """
    if not _is_private_channel(channel):
        return True
    if sender == agent_id:
        return True
    return agent_id in channel.split(":")


def _validate_channel_name(
    channel: str, token_store: TokenStore | None = None
) -> tuple[dict, int] | None:
    """Validate channel name. Returns error tuple or None if OK."""
    is_reserved = any(channel.startswith(p) for p in _RESERVED_PREFIXES)

    if is_reserved:
        if not _SYSTEM_CHANNEL_RE.match(channel):
            return {"error": "Bad Request", "message": "Invalid system channel name"}, 400
        return None

    # Check for custom registered prefix (channel contains colon)
    if ":" in channel:
        prefix = channel.split(":")[0]
        if token_store and token_store.is_registered_prefix(prefix):
            if len(channel) < 3 or len(channel) > 64 or channel != channel.lower():
                return {"error": "Bad Request", "message": "Invalid prefixed channel name"}, 400
            return None
        return {
            "error": "Bad Request",
            "message": f"Prefix '{prefix}:' is not registered. Register custom prefixes via admin panel.",
        }, 400

    # Plain user channel — no colons
    if len(channel) < 3 or not _USER_CHANNEL_RE.match(channel):
        return {
            "error": "Bad Request",
            "message": "Channel name must be 3-64 chars, start with letter, end with letter/digit, "
            "lowercase, no colons (use registered prefixes for namespaced channels)",
        }, 400
    if not any(c.isalpha() for c in channel):
        return {
            "error": "Bad Request",
            "message": "Channel name must contain at least one letter",
        }, 400
    if channel != channel.lower():
        return {"error": "Bad Request", "message": "Channel name must be lowercase"}, 400
    if _NO_CONSECUTIVE_SPECIALS.search(channel):
        return {
            "error": "Bad Request",
            "message": "Channel name cannot contain consecutive special characters",
        }, 400

    return None


def _validate_and_auth_publish(
    auth_result: Any,
    sender: str,
    channel: str,
    msg_type: str = "",
    token_store: TokenStore | None = None,
) -> tuple[dict, int] | None:
    """Validate channel name and enforce publish auth.

    Returns error tuple or None if OK.
    """
    err = _validate_channel_name(channel, token_store=token_store)
    if err:
        return err

    if not isinstance(auth_result, str):
        return None

    # Sender identity: token must match the declared sender
    if sender != auth_result:
        return {
            "error": "Forbidden",
            "message": f"Token bound to '{auth_result}', cannot publish as '{sender}'",
        }, 403

    # System channels: agents can only write to their own system channels
    if channel.startswith("_system:"):
        # Allow: _system:agents with msg_type="presence" (SDK _announce())
        # Allow: _system:cursors:{agent_id} (own cursor persistence)
        # Allow: _system:registry (self-registration)
        # Deny: _system:agents with arbitrary msg_types
        allowed_system = (
            f"_system:cursors:{auth_result}",
            "_system:registry",
        )
        if channel == "_system:agents" and msg_type == "presence":
            pass  # allow SDK presence announcement
        elif channel not in allowed_system:
            return {
                "error": "Forbidden",
                "message": f"Channel '{channel}' is reserved for system use",
            }, 403

    # Broadcast channels: only supertokens can write
    if channel.startswith("broadcast:"):
        return {
            "error": "Forbidden",
            "message": f"Channel '{channel}' is reserved for admin use",
        }, 403

    # Channel ownership: notebook:X and memory:X are private to agent X
    for prefix in _PRIVATE_CHANNEL_PREFIXES:
        if channel.startswith(prefix):
            owner = channel[len(prefix) :]
            if owner != auth_result:
                return {
                    "error": "Forbidden",
                    "message": f"Channel '{channel}' belongs to agent '{owner}'",
                }, 403

    return None


def _fmt_bytes(n: int) -> str:
    """Format byte count as human-readable string (e.g. 256KB, 16KB, 1MB)."""
    if n >= 1024 * 1024 and n % (1024 * 1024) == 0:
        return f"{n // (1024 * 1024)}MB"
    if n >= 1024 and n % 1024 == 0:
        return f"{n // 1024}KB"
    return f"{n} bytes"


_PUBLISH_REQUIRED = ("channel", "sender", "msg_type", "payload")
_MAX_PAYLOAD_BYTES = 256 * 1024  # 256 KB
_MAX_MSG_TYPE_LEN = 64
_MAX_METADATA_BYTES = 16384


def _validate_metadata(metadata: Any) -> tuple[dict, int] | None:
    """Validate metadata type and size. Returns error tuple or None if OK."""
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        return {
            "error": "Bad Request",
            "message": "metadata must be a JSON object or null",
        }, 400
    try:
        meta_json = json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError):
        return {
            "error": "Bad Request",
            "message": "Metadata must be JSON-serializable",
        }, 400
    if len(meta_json.encode()) > _MAX_METADATA_BYTES:
        return {
            "error": "Bad Request",
            "message": f"Metadata exceeds maximum size of {_fmt_bytes(_MAX_METADATA_BYTES)}",
        }, 400
    return None


def _validate_tags(metadata: dict) -> tuple[dict, int] | None:
    """Validate and normalise ``metadata["tags"]`` in-place.

    Returns an error tuple or *None* if valid.
    """
    tags = metadata["tags"]
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return {
            "error": "Bad Request",
            "message": "Tags must be a list of strings",
        }, 400
    tags = [t.strip() for t in tags]
    if not all(tags):
        return {
            "error": "Bad Request",
            "message": "Tags must be non-empty strings",
        }, 400
    if len(tags) > 20:
        return {"error": "Bad Request", "message": "Maximum 20 tags allowed"}, 400
    if any(len(t) > 64 for t in tags):
        return {
            "error": "Bad Request",
            "message": "Each tag must be at most 64 characters",
        }, 400
    metadata["tags"] = tags
    return None


def _validate_payload(data: dict) -> tuple[dict, int] | None:
    """Validate ``data["payload"]``.

    Returns an error tuple or *None* if valid.
    """
    payload = data["payload"]
    if not isinstance(payload, str) or not payload.strip():
        return {
            "error": "Bad Request",
            "message": "Payload must be a non-empty string",
        }, 400
    if "\x00" in payload:
        return {
            "error": "Bad Request",
            "message": "Payload must not contain null bytes",
        }, 400
    if len(payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        return {
            "error": "Bad Request",
            "message": f"Payload exceeds maximum size of {_fmt_bytes(_MAX_PAYLOAD_BYTES)}",
        }, 400
    return None


def _validate_msg_type(data: dict) -> tuple[dict, int] | None:
    """Validate and normalise ``data["msg_type"]`` in-place.

    Returns an error tuple or *None* if valid.
    """
    msg_type = data["msg_type"]
    if not isinstance(msg_type, str) or not msg_type.strip():
        return {
            "error": "Bad Request",
            "message": "msg_type must be a non-empty string",
        }, 400
    if "\x00" in msg_type:
        return {
            "error": "Bad Request",
            "message": "msg_type must not contain null bytes",
        }, 400
    data["msg_type"] = msg_type.strip()
    if len(data["msg_type"]) > _MAX_MSG_TYPE_LEN:
        return {
            "error": "Bad Request",
            "message": f"msg_type must be at most {_MAX_MSG_TYPE_LEN} characters",
        }, 400
    return None


def _parse_publish_body(request: Any) -> dict | tuple:
    """Parse and validate the publish request body.

    Returns the validated data dict, or an (error_dict, status) tuple.
    """
    try:
        data = request.json()
    except Exception:
        return {
            "error": "Bad Request",
            "message": "Request body must be valid JSON",
        }, 400
    if not isinstance(data, dict):
        return {
            "error": "Bad Request",
            "message": "Request body must be a JSON object",
        }, 400

    missing = [f for f in _PUBLISH_REQUIRED if f not in data]
    if missing:
        return {
            "error": "Bad Request",
            "message": f"Missing: {', '.join(missing)}",
        }, 400

    payload_err = _validate_payload(data)
    if payload_err:
        return payload_err

    msg_type_err = _validate_msg_type(data)
    if msg_type_err:
        return msg_type_err

    metadata = data.get("metadata")
    meta_err = _validate_metadata(metadata)
    if meta_err:
        return meta_err

    if isinstance(metadata, dict) and "tags" in metadata:
        tags_err = _validate_tags(metadata)
        if tags_err:
            return tags_err

    if "\x00" in data["channel"]:
        return {
            "error": "Bad Request",
            "message": "Channel must not contain null bytes",
        }, 400

    sender = data["sender"]
    if not isinstance(sender, str) or "\x00" in sender:
        return {
            "error": "Bad Request",
            "message": "Sender must be a string without null bytes",
        }, 400

    return data


def _parse_query_limit(raw: str | None, max_limit: int) -> int | tuple[dict, int]:
    """Parse and validate query limit parameter.

    Returns int limit on success, or (error_dict, status) tuple.
    """
    try:
        limit = int(raw or "100")
    except ValueError:
        return {
            "error": "Bad Request",
            "message": "'limit' must be an integer",
        }, 400
    if limit < 1:
        return {
            "error": "Bad Request",
            "message": "'limit' must be at least 1",
        }, 400
    return min(limit, max_limit)


def _parse_query_offset(raw: str | None) -> int | tuple[dict, int]:
    """Parse and validate query offset parameter.

    Returns int offset on success, or (error_dict, status) tuple.
    """
    if raw is None:
        return 0
    try:
        offset = int(raw)
    except ValueError:
        return {
            "error": "Bad Request",
            "message": "'offset' must be an integer",
        }, 400
    if offset < 0:
        return {
            "error": "Bad Request",
            "message": "'offset' must be non-negative",
        }, 400
    return offset


def _parse_query_params(request: Any, max_limit: int) -> dict:
    """Extract and validate query parameters (channel, after, limit, order, offset).

    Returns dict with 'channel', 'after', 'limit', 'order', 'offset' on success,
    or dict with 'error' key and '_status' key on failure.
    """
    channel = (request.query_params.get("channel") or [None])[0]
    if not channel:
        return {
            "error": "Bad Request",
            "message": "Query param 'channel' required",
            "_status": 400,
        }

    after = (request.query_params.get("after") or [None])[0]
    msg_type = (request.query_params.get("msg_type") or [None])[0]
    limit = _parse_query_limit(
        (request.query_params.get("limit") or [None])[0],
        max_limit,
    )
    if not isinstance(limit, int):
        return {"error": limit[0]["error"], "message": limit[0]["message"], "_status": 400}

    offset = _parse_query_offset(
        (request.query_params.get("offset") or [None])[0],
    )
    if not isinstance(offset, int):
        return {"error": offset[0]["error"], "message": offset[0]["message"], "_status": 400}

    order = (request.query_params.get("order") or ["oldest"])[0]
    if order not in ("oldest", "newest"):
        return {
            "error": "Bad Request",
            "message": "'order' must be 'oldest' or 'newest'",
            "_status": 400,
        }

    thread_id = (request.query_params.get("thread_id") or [None])[0]

    return {
        "channel": channel,
        "after": after,
        "limit": limit,
        "msg_type": msg_type,
        "order": order,
        "thread_id": thread_id,
        "offset": offset,
    }


async def _handle_registry_lookup(request: Any, bus: Any) -> dict | tuple:
    """Handle /v1/registry/lookup requests."""
    agent_id = (request.query_params.get("agent_id") or [None])[0]
    if not agent_id:
        return {
            "error": "Bad Request",
            "message": "Query param 'agent_id' required",
        }, 400

    msgs = await asyncio.to_thread(bus.query, "_system:registry", limit=1000)
    for m in reversed(msgs):
        if m.sender == agent_id and m.msg_type == "register":
            return {"found": True, "agent_id": agent_id, "metadata": m.metadata}
    return {"found": False, "agent_id": agent_id}


async def _dm_unregistered_warning(bus: Any, channel: str, sender: str) -> str | None:
    """Return a warning string if a DM targets an unregistered agent."""
    if not channel.startswith("dm:"):
        return None
    parts = channel.split(":")
    if len(parts) != 3:
        return None
    other = parts[1] if parts[2] == sender else parts[2]
    status = await asyncio.to_thread(bus.agent_status, other)
    if status is None:
        return f"target agent '{other}' is not registered; message stored but may never be read"
    return None


async def _handle_queue_status(request: Any, bus: Any) -> dict | tuple:
    """Handle /v1/queue/status requests."""
    message_id = (request.query_params.get("message_id") or [None])[0]
    if not message_id:
        return {
            "error": "Bad Request",
            "message": "Query param 'message_id' required",
        }, 400
    result = await asyncio.to_thread(bus.queue_status, message_id)
    if result is None:
        return {"error": "Not Found", "message": "No queue status for this message"}, 404
    return {"found": True, "status": result}


def _parse_queue_body(request: Any, required_field: str) -> tuple[dict, tuple | None]:
    """Parse JSON body and validate a required field for queue endpoints."""
    try:
        data = request.json()
    except Exception:
        return {}, ({"error": "Bad Request", "message": "Invalid JSON"}, 400)
    if not isinstance(data, dict):
        return {}, ({"error": "Bad Request", "message": "Invalid JSON"}, 400)
    value = data.get(required_field, "").strip()
    if not value:
        return {}, ({"error": "Bad Request", "message": f"Missing '{required_field}'"}, 400)
    data[required_field] = value
    return data, None


def _resolve_claimed_by(data: dict) -> tuple[str, tuple | None]:
    """Resolve claimed_by from auth context or request body."""
    auth_result = _auth_result_var.get()
    if auth_result is False:
        return "", ({"error": "Unauthorized", "message": "Valid token required"}, 401)
    if isinstance(auth_result, str):
        return auth_result, None
    claimed_by = data.get("claimed_by", "").strip()
    if not claimed_by:
        return "", ({"error": "Bad Request", "message": "Missing 'claimed_by'"}, 400)
    return claimed_by, None


def _claim_result_to_dict(result: Any) -> dict[str, Any]:
    """Serialize a ClaimResult to a dict."""
    d: dict[str, Any] = {
        "message": _msg_to_dict(result.message),
        "status": result.status,
        "claimed_by": result.claimed_by,
        "claimed_at": result.claimed_at,
    }
    if result.lease_until is not None:
        d["lease_until"] = result.lease_until
    return d


def _msg_to_dict(m: Message) -> dict[str, Any]:
    """Serialize a Message to a dict."""
    d: dict[str, Any] = {
        "id": m.id,
        "channel": m.channel,
        "sender": m.sender,
        "msg_type": m.msg_type,
        "payload": m.payload,
        "timestamp": m.timestamp,
        "metadata": m.metadata,
    }
    if m.parent_id is not None:
        d["parent_id"] = m.parent_id
    if m.thread_id is not None:
        d["thread_id"] = m.thread_id
    return d


class HttpFrontend:
    """HTTP frontend — REST API + SSE push notifications.

    Exposes a Bus over HTTP so remote MansioClient instances
    can connect via HttpTransport.

    Args:
        host: Bind address. Default "127.0.0.1".
        port: Bind port. Default 8742.
        cors_origin: Access-Control-Allow-Origin value. Default "*".
        max_body_bytes: Maximum request body size in bytes. Default 1MB.
        max_query_limit: Maximum query limit parameter. Default 10000.
        token_store: Optional TokenStore for API authentication. None disables auth.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8742,
        cors_origin: str = "*",
        max_body_bytes: int = 1_048_576,
        max_query_limit: int = 10_000,
        token_store: TokenStore | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._cors_origin = cors_origin
        self._max_query_limit = max_query_limit
        self._token_store = token_store
        self._bus: Bus | None = None
        self._app = App(max_body_size=max_body_bytes)

    def attach(self, bus: Bus) -> None:
        """Bind this frontend to a Bus and register routes.

        Args:
            bus: The Bus to expose over HTTP.

        Raises:
            RuntimeError: If already attached.
        """
        if self._bus is not None:
            raise RuntimeError("Frontend already attached to a bus")
        self._bus = bus
        self._setup_middleware()
        self._setup_api_routes()
        self._setup_deletion_routes()
        self._setup_queue_routes()
        self._setup_presence_routes()
        self._setup_channel_acl_routes()
        self._setup_sse_route()

    def serve_forever(self) -> None:
        """Start the HTTP server. Blocks until shutdown().

        Raises:
            RuntimeError: If not attached to a Bus.
        """
        if self._bus is None:
            raise RuntimeError("Must call attach(bus) before serve_forever()")
        self._app.run(self._host, self._port)

    def shutdown(self) -> None:
        """Stop the server. Thread-safe."""
        self._app.shutdown()

    @property
    def address(self) -> tuple[str, int]:
        """Return the (host, port) this frontend is bound to."""
        h = self._app.host or self._host
        p = self._app.port if self._app.port is not None else self._port
        return (h, p)

    def __repr__(self) -> str:
        addr = f"{self._host}:{self._port}"
        status = "attached" if self._bus else "detached"
        auth = "auth" if self._token_store else "no-auth"
        return f"HttpFrontend({addr}, {status}, {auth})"

    # ── Middleware ─────────────────────────────────────────────────

    def _setup_middleware(self) -> None:
        """Register auth and CORS middleware."""
        cors = self._cors_origin
        token_store = self._token_store

        @self._app.before_request
        async def cors_and_auth(request: Request) -> Response | None:
            # CORS preflight
            if request.method == "OPTIONS":
                return Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": cors,
                        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type, Authorization",
                    },
                )

            # Token auth for /v1/* paths
            path = request.path
            if not path.startswith("/v1") and path != "/health":
                return None

            if path in _PUBLIC_PATHS:
                return None

            if token_store is None or not await asyncio.to_thread(token_store.has_tokens):
                _auth_result_var.set(True)
                return None

            auth_header = request.headers.get("authorization", "")
            if not auth_header:
                return JSONResponse(
                    {"error": "Unauthorized", "message": "Missing Authorization header"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mansio"'},
                )

            parts = auth_header.split(" ", 1)
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return JSONResponse(
                    {"error": "Unauthorized", "message": "Expected 'Bearer <token>'"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mansio"'},
                )

            result = await asyncio.to_thread(token_store.validate, parts[1])
            if result is False:
                return JSONResponse(
                    {"error": "Unauthorized", "message": "Invalid or expired token"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mansio"'},
                )

            # result is str (agent_id) or None (supertoken)
            _auth_result_var.set(result)
            return None

        @self._app.after_request
        async def add_cors(
            request: Request, response: Response | StreamingResponse
        ) -> Response | StreamingResponse:
            if hasattr(response, "headers"):
                response.headers.setdefault("Access-Control-Allow-Origin", cors)
            return response

    # ── Routes ────────────────────────────────────────────────────

    def _setup_api_routes(self) -> None:
        """Register REST API routes (non-SSE)."""
        bus = self._bus
        assert bus is not None
        max_query_limit = self._max_query_limit

        @self._app.get("/health")
        async def health(request: Request) -> dict:
            return {"status": "ok"}

        @self._app.post("/v1/publish")
        async def publish(request: Request) -> dict | tuple:
            parsed = _parse_publish_body(request)
            if not isinstance(parsed, dict):
                return parsed  # error tuple
            data = parsed

            auth_result = _auth_result_var.get()
            data["channel"] = data["channel"].strip()
            data["sender"] = data["sender"].strip()

            auth_error = _validate_and_auth_publish(
                auth_result,
                data["sender"],
                data["channel"],
                msg_type=data["msg_type"],
                token_store=self._token_store,
            )
            if auth_error:
                return auth_error

            try:
                msg_id = await asyncio.to_thread(
                    bus.publish,
                    channel=data["channel"],
                    sender=data["sender"],
                    msg_type=data["msg_type"],
                    payload=data["payload"],
                    metadata=data.get("metadata"),
                    queue=bool(data.get("queue")),
                    parent_id=data.get("parent_id"),
                )
            except ValueError as e:
                return {
                    "error": "Not Found" if "not found" in str(e) else "Bad Request",
                    "message": str(e),
                }, 404 if "not found" in str(e) else 400
            result: dict[str, object] = {"message_id": msg_id}

            warning = await _dm_unregistered_warning(bus, data["channel"], data["sender"])
            if warning:
                result["warning"] = warning

            return result

        @self._app.get("/v1/query")
        async def query(request: Request) -> dict | tuple:
            qp = _parse_query_params(request, max_query_limit)
            if "error" in qp:
                status = qp.pop("_status", 400)
                return qp, status
            channel, after, limit = qp["channel"], qp["after"], qp["limit"]
            offset = qp.get("offset", 0)

            msgs = await asyncio.to_thread(
                bus.query,
                channel,
                after=after,
                limit=limit,
                msg_type=qp.get("msg_type"),
                order=qp.get("order", "oldest"),
                thread_id=qp.get("thread_id"),
                offset=offset,
            )

            auth_result = _auth_result_var.get()
            if isinstance(auth_result, str):
                msgs = [m for m in msgs if _agent_involved(auth_result, m.channel, m.sender)]

            total = await asyncio.to_thread(bus.message_count, channel)
            has_more = (offset + len(msgs)) < total

            return {
                "messages": [_msg_to_dict(m) for m in msgs],
                "count": len(msgs),
                "total": total,
                "offset": offset,
                "has_more": has_more,
            }

        @self._app.get("/v1/channels")
        async def channels(request: Request) -> dict:
            detail_param = (request.query_params.get("detail") or ["false"])[0]
            want_detail = detail_param.lower() == "true"

            auth_result = _auth_result_var.get()

            if want_detail:
                all_detail = await asyncio.to_thread(bus.channels_detail)
                if isinstance(auth_result, str):
                    all_detail = [
                        ch
                        for ch in all_detail
                        if not _is_private_channel(ch["name"])
                        or auth_result in ch["name"].split(":")
                    ]
                return {"channels": all_detail}

            all_channels: list[str] = await asyncio.to_thread(bus.channels)
            if isinstance(auth_result, str):
                all_channels = [
                    ch
                    for ch in all_channels
                    if not _is_private_channel(ch) or auth_result in ch.split(":")
                ]
            return {"channels": all_channels}

        @self._app.get("/v1/auth/check")
        async def auth_check(request: Request) -> dict:
            has_tokens = bool(
                self._token_store and await asyncio.to_thread(self._token_store.has_tokens)
            )
            return {
                "require_auth": bus.require_auth,
                "token_auth_enabled": has_tokens,
            }

        @self._app.get("/v1/registry/lookup")
        async def registry_lookup(request: Request) -> dict | tuple:
            return await _handle_registry_lookup(request, bus)

    def _setup_deletion_routes(self) -> None:
        """Register channel and message deletion routes."""
        self._setup_delete_channel_route()
        self._setup_delete_message_route()
        self._setup_admin_cleanup_route()

    def _setup_delete_channel_route(self) -> None:
        """Register DELETE /v1/channels/<channel_name>."""
        bus = self._bus
        assert bus is not None

        @self._app.delete("/v1/channels/<channel_name>")
        async def delete_channel(request: Request, channel_name: str = "") -> dict | tuple:
            if not channel_name:
                return {"error": "Bad Request", "message": "Channel name required"}, 400

            auth_result = _auth_result_var.get()

            # System channels: only supertoken or no-auth can delete
            if channel_name.startswith("_system:") and isinstance(auth_result, str):
                return {
                    "error": "Forbidden",
                    "message": f"Channel '{channel_name}' is a system channel; "
                    "only admin tokens can delete system channels",
                }, 403

            # Scoped tokens: can only delete their own private channels
            if isinstance(auth_result, str):
                if _is_private_channel(channel_name):
                    if auth_result not in channel_name.split(":"):
                        return {
                            "error": "Forbidden",
                            "message": f"Token for '{auth_result}' cannot delete "
                            f"channel '{channel_name}'",
                        }, 403
                else:
                    # Public/user channels: scoped tokens cannot delete
                    return {
                        "error": "Forbidden",
                        "message": "Only admin tokens can delete public channels",
                    }, 403

            try:
                count = await asyncio.to_thread(bus.delete_channel, channel_name)
            except NotImplementedError:
                return {
                    "error": "Not Implemented",
                    "message": "Backend does not support deletion",
                }, 501

            if count == 0:
                return {
                    "error": "Not Found",
                    "message": f"Channel '{channel_name}' not found or already empty",
                }, 404

            return {"deleted": count, "channel": channel_name}

    def _setup_delete_message_route(self) -> None:
        """Register DELETE /v1/messages/<message_id>."""
        bus = self._bus
        assert bus is not None

        @self._app.delete("/v1/messages/<message_id>")
        async def delete_message(request: Request, message_id: str = "") -> dict | tuple:
            if not message_id:
                return {"error": "Bad Request", "message": "Message ID required"}, 400

            auth_result = _auth_result_var.get()

            # For scoped tokens, verify the message belongs to the agent
            if isinstance(auth_result, str):
                found_msg = await asyncio.to_thread(bus.get_message, message_id)

                if found_msg is None:
                    return {
                        "error": "Not Found",
                        "message": f"Message '{message_id}' not found",
                    }, 404

                if found_msg.sender != auth_result:
                    return {
                        "error": "Forbidden",
                        "message": f"Token for '{auth_result}' cannot delete messages "
                        f"from sender '{found_msg.sender}'",
                    }, 403

            try:
                deleted = await asyncio.to_thread(bus.delete_message, message_id)
            except NotImplementedError:
                return {
                    "error": "Not Implemented",
                    "message": "Backend does not support deletion",
                }, 501

            if not deleted:
                return {
                    "error": "Not Found",
                    "message": f"Message '{message_id}' not found",
                }, 404

            return {"deleted": True, "message_id": message_id}

    def _setup_admin_cleanup_route(self) -> None:
        """Register POST /v1/admin/channels/cleanup."""
        bus = self._bus
        assert bus is not None

        @self._app.post("/v1/admin/channels/cleanup")
        async def admin_cleanup(request: Request) -> dict | tuple:
            auth_result = _auth_result_var.get()

            # Admin only: supertoken (None) or no-auth mode (True)
            if isinstance(auth_result, str):
                return {
                    "error": "Forbidden",
                    "message": "Admin endpoints require a supertoken",
                }, 403

            try:
                data = request.json()
            except Exception:
                return {
                    "error": "Bad Request",
                    "message": "Request body must be valid JSON",
                }, 400

            if not isinstance(data, dict):
                return {
                    "error": "Bad Request",
                    "message": "Request body must be a JSON object",
                }, 400

            pattern = data.get("pattern", "").strip()
            if not pattern:
                return {
                    "error": "Bad Request",
                    "message": "'pattern' field is required (e.g. 'test:*')",
                }, 400

            older_than = data.get("older_than")

            all_channels = await asyncio.to_thread(bus.channels)
            matched = _match_channels(all_channels, pattern)

            # Protect system channels from bulk deletion
            matched = [ch for ch in matched if not ch.startswith("_system:")]

            if older_than:
                # Filter by last activity time
                try:
                    detail_list = await asyncio.to_thread(bus.channels_detail)
                except NotImplementedError:
                    detail_list = []
                detail_map = {d["name"]: d for d in detail_list}
                matched = [
                    ch
                    for ch in matched
                    if ch in detail_map and detail_map[ch].get("last_activity", "") < older_than
                ]

            total_deleted = 0
            channels_deleted: list[str] = []

            try:
                for ch in matched:
                    count = await asyncio.to_thread(bus.delete_channel, ch)
                    total_deleted += count
                    channels_deleted.append(ch)
            except NotImplementedError:
                return {
                    "error": "Not Implemented",
                    "message": "Backend does not support deletion",
                }, 501

            return {
                "channels_deleted": len(channels_deleted),
                "messages_deleted": total_deleted,
                "channels": channels_deleted,
            }

    def _setup_queue_routes(self) -> None:
        """Register queue (claim/ack/status) routes."""
        bus = self._bus
        assert bus is not None

        # ── Shared handlers ───────────────────────────────────────

        async def _handle_claim(request: Request) -> dict | tuple:
            data, err = _parse_queue_body(request, "channel")
            if err:
                return err
            claimed_by, err = _resolve_claimed_by(data)
            if err:
                return err

            lease = int(data.get("lease_seconds", 300))
            result = await asyncio.to_thread(
                bus.queue_claim, data["channel"], claimed_by, lease_seconds=lease
            )
            if result is None:
                return {"claimed": False}
            return {"claimed": True, "result": _claim_result_to_dict(result)}

        async def _handle_ack(request: Request) -> dict | tuple:
            data, err = _parse_queue_body(request, "message_id")
            if err:
                return err
            claimed_by, err = _resolve_claimed_by(data)
            if err:
                return err

            result = await asyncio.to_thread(bus.queue_ack, data["message_id"], claimed_by)
            if result is None:
                return {"acked": False}
            return {"acked": True, "result": _claim_result_to_dict(result)}

        # ── Canonical paths (/v1/queue/*) ─────────────────────────

        @self._app.post("/v1/queue/claim")
        async def queue_claim(request: Request) -> dict | tuple:
            return await _handle_claim(request)

        @self._app.post("/v1/queue/ack")
        async def queue_ack(request: Request) -> dict | tuple:
            return await _handle_ack(request)

        @self._app.get("/v1/queue/status")
        async def queue_status(request: Request) -> dict | tuple:
            return await _handle_queue_status(request, bus)

    def _setup_presence_routes(self) -> None:
        """Register presence (heartbeat / roster) routes."""
        bus = self._bus
        assert bus is not None

        @self._app.post("/v1/presence/heartbeat")
        async def presence_heartbeat(request: Request) -> dict | tuple:
            body = request.json
            if not body or not isinstance(body, dict):
                return {"error": "JSON body required"}, 400
            agent_id = body.get("agent_id", "").strip()
            if not agent_id:
                return {"error": "agent_id required"}, 400
            metadata = body.get("metadata")
            meta_err = _validate_metadata(metadata)
            if meta_err:
                return meta_err
            await asyncio.to_thread(bus.heartbeat, agent_id, metadata)
            return {"ok": True}

        @self._app.get("/v1/presence")
        async def presence_list(request: Request) -> dict:
            timeout = int((request.query_params.get("timeout") or ["120"])[0])
            agents = await asyncio.to_thread(bus.agents, timeout)
            return {
                "agents": [
                    {
                        "agent_id": a.agent_id,
                        "status": a.status,
                        "last_seen": a.last_seen,
                        "metadata": a.metadata,
                    }
                    for a in agents
                ]
            }

        @self._app.get("/v1/presence/<agent_id>")
        async def presence_agent(request: Request) -> dict | tuple:
            agent_id = request.path_params.get("agent_id", "")
            timeout = int((request.query_params.get("timeout") or ["120"])[0])
            result = await asyncio.to_thread(bus.agent_status, agent_id, timeout)
            if result is None:
                return {"error": "agent not found"}, 404
            return {
                "agent_id": result.agent_id,
                "status": result.status,
                "last_seen": result.last_seen,
                "metadata": result.metadata,
            }

    def _setup_channel_acl_routes(self) -> None:
        """Register channel metadata and ACL management routes."""
        self._setup_channel_meta_routes()
        self._setup_acl_crud_routes()

    def _setup_channel_meta_routes(self) -> None:
        """Register channel metadata routes (create, get)."""
        bus = self._bus
        assert bus is not None

        @self._app.post("/v1/channels")
        async def create_channel(request: Request) -> dict | tuple:
            body = request.json()
            name = body.get("name", "").strip()
            if not name:
                return {"error": "Bad Request", "message": "'name' is required"}, 400

            auth_result = _auth_result_var.get()
            owner = body.get("owner", "")
            if isinstance(auth_result, str):
                owner = auth_result  # scoped tokens own what they create
            elif not owner:
                return {
                    "error": "Bad Request",
                    "message": "'owner' is required for supertoken/no-auth",
                }, 400

            visibility = body.get("visibility", "public")
            if visibility not in ("public", "private"):
                return {
                    "error": "Bad Request",
                    "message": "visibility must be 'public' or 'private'",
                }, 400

            try:
                meta = await asyncio.to_thread(
                    bus.create_channel, name, owner, visibility=visibility
                )
            except NotImplementedError:
                return {
                    "error": "Not Implemented",
                    "message": "Backend does not support channel management",
                }, 501
            except ValueError as exc:
                return {"error": "Conflict", "message": str(exc)}, 409

            return {
                "channel": {
                    "name": meta.name,
                    "owner": meta.owner,
                    "visibility": meta.visibility,
                    "created_at": meta.created_at,
                }
            }, 201

        @self._app.get("/v1/channels/<channel>/meta")
        async def get_channel_meta(request: Request, channel: str = "") -> dict | tuple:
            try:
                meta = await asyncio.to_thread(bus.get_channel_meta, channel)
            except NotImplementedError:
                return {
                    "error": "Not Implemented",
                    "message": "Backend does not support channel management",
                }, 501

            if meta is None:
                return {"error": "Not Found", "message": f"Channel '{channel}' not found"}, 404

            return {
                "channel": {
                    "name": meta.name,
                    "owner": meta.owner,
                    "visibility": meta.visibility,
                    "created_at": meta.created_at,
                }
            }

    def _setup_acl_crud_routes(self) -> None:
        """Register ACL management routes (get, set, add, remove)."""
        self._setup_acl_read_route()
        self._setup_acl_write_routes()

    def _setup_acl_read_route(self) -> None:
        """Register GET /v1/channels/<channel>/acl."""
        bus = self._bus
        assert bus is not None

        @self._app.get("/v1/channels/<channel>/acl")
        async def get_acl(request: Request, channel: str = "") -> dict | tuple:
            denied = await _require_acl_admin(bus, channel)
            if denied:
                return denied

            try:
                entries = await asyncio.to_thread(bus.get_acl, channel)
            except NotImplementedError:
                return {
                    "error": "Not Implemented",
                    "message": "Backend does not support channel management",
                }, 501

            return {
                "acl": [
                    {
                        "channel": e.channel,
                        "agent_id": e.agent_id,
                        "permission": e.permission,
                        "granted_at": e.granted_at,
                        "granted_by": e.granted_by,
                    }
                    for e in entries
                ]
            }

    def _setup_acl_write_routes(self) -> None:
        """Register PUT/POST/DELETE ACL mutation routes."""
        self._setup_acl_set_route()
        self._setup_acl_add_route()
        self._setup_acl_remove_route()

    def _setup_acl_set_route(self) -> None:
        """Register PUT /v1/channels/<channel>/acl."""
        bus = self._bus
        assert bus is not None

        @self._app.put("/v1/channels/<channel>/acl")
        async def set_acl(request: Request, channel: str = "") -> dict | tuple:
            denied = await _require_acl_admin(bus, channel)
            if denied:
                return denied

            body = request.json()
            raw_entries = body.get("acl", [])
            if not isinstance(raw_entries, list):
                return {"error": "Bad Request", "message": "'acl' must be a list"}, 400

            from mansio.types import ACLEntry

            auth_result = _auth_result_var.get()
            now = _now_iso()
            granted_by = auth_result if isinstance(auth_result, str) else None
            entries = [
                ACLEntry(
                    channel=channel,
                    agent_id=raw["agent_id"],
                    permission=raw.get("permission", "read"),
                    granted_at=now,
                    granted_by=granted_by,
                )
                for raw in raw_entries
            ]

            try:
                await asyncio.to_thread(bus.set_acl, channel, entries)
            except NotImplementedError:
                return {
                    "error": "Not Implemented",
                    "message": "Backend does not support channel management",
                }, 501

            return {"status": "ok", "count": len(entries)}

    def _setup_acl_add_route(self) -> None:
        """Register POST /v1/channels/<channel>/acl."""
        bus = self._bus
        assert bus is not None

        @self._app.post("/v1/channels/<channel>/acl")
        async def add_acl_entry(request: Request, channel: str = "") -> dict | tuple:
            denied = await _require_acl_admin(bus, channel)
            if denied:
                return denied

            body = request.json()
            agent_id = body.get("agent_id", "").strip()
            if not agent_id:
                return {"error": "Bad Request", "message": "'agent_id' is required"}, 400

            permission = body.get("permission", "read")
            if permission not in ("read", "write", "admin"):
                return {
                    "error": "Bad Request",
                    "message": "permission must be 'read', 'write', or 'admin'",
                }, 400

            from mansio.types import ACLEntry

            auth_result = _auth_result_var.get()
            now = _now_iso()
            granted_by = auth_result if isinstance(auth_result, str) else None
            entry = ACLEntry(
                channel=channel,
                agent_id=agent_id,
                permission=permission,
                granted_at=now,
                granted_by=granted_by,
            )

            try:
                await asyncio.to_thread(bus.add_acl_entry, entry)
            except NotImplementedError:
                return {
                    "error": "Not Implemented",
                    "message": "Backend does not support channel management",
                }, 501

            return {
                "status": "ok",
                "entry": {
                    "channel": entry.channel,
                    "agent_id": entry.agent_id,
                    "permission": entry.permission,
                },
            }, 201

    def _setup_acl_remove_route(self) -> None:
        """Register DELETE /v1/channels/<channel>/acl/<agent_id>."""
        bus = self._bus
        assert bus is not None

        @self._app.delete("/v1/channels/<channel>/acl/<agent_id>")
        async def remove_acl_entry(
            request: Request, channel: str = "", agent_id: str = ""
        ) -> dict | tuple:
            denied = await _require_acl_admin(bus, channel)
            if denied:
                return denied

            try:
                removed = await asyncio.to_thread(bus.remove_acl_entry, channel, agent_id)
            except NotImplementedError:
                return {
                    "error": "Not Implemented",
                    "message": "Backend does not support channel management",
                }, 501

            if not removed:
                return {
                    "error": "Not Found",
                    "message": f"No ACL entry for '{agent_id}' on '{channel}'",
                }, 404

            return {"status": "ok"}

    def _setup_sse_route(self) -> None:
        """Register the SSE subscribe routes."""
        bus = self._bus
        assert bus is not None
        cors = self._cors_origin

        async def _handle_subscribe(
            request: Request, ch_list: list[str]
        ) -> StreamingResponse | tuple:
            """Shared SSE handler for all subscribe endpoints."""
            if not ch_list:
                return {
                    "error": "Bad Request",
                    "message": "At least one channel required",
                }, 400

            auth_result = _auth_result_var.get()
            error = _check_subscribe_access(auth_result, ch_list)
            if error:
                return error

            # Parse Last-Event-ID for cursor-based resume
            last_event_id = request.headers.get("last-event-id", "").strip() or None

            q, sub_ids, drop_lock, drop_counter = _setup_sse_subscriptions(
                bus, ch_list, auth_result
            )

            return StreamingResponse(
                _sse_event_generator(
                    q,
                    sub_ids,
                    bus,
                    ch_list,
                    auth_result,
                    last_event_id,
                    drop_lock,
                    drop_counter,
                ),
                content_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Access-Control-Allow-Origin": cors,
                },
            )

        @self._app.get("/v1/subscribe")
        async def subscribe(request: Request) -> StreamingResponse | tuple:
            """Multi-channel SSE endpoint.

            Accepts repeated ``channel`` params and/or a comma-separated
            ``channels`` param::

                GET /v1/subscribe?channel=ch1&channel=ch2
                GET /v1/subscribe?channels=ch1,ch2
            """
            ch_list = list(request.query_params.get("channel", []))
            # Also accept comma-separated "channels" param
            for csv in request.query_params.get("channels", []):
                ch_list.extend(c.strip() for c in csv.split(",") if c.strip())
            # Deduplicate while preserving order
            seen: set[str] = set()
            deduped: list[str] = []
            for ch in ch_list:
                if ch not in seen:
                    seen.add(ch)
                    deduped.append(ch)
            return await _handle_subscribe(request, deduped)

        @self._app.get("/v1/channels/<channel>/subscribe")
        async def subscribe_channel(
            request: Request, channel: str = ""
        ) -> StreamingResponse | tuple:
            """Per-channel SSE convenience endpoint."""
            if not channel:
                return {
                    "error": "Bad Request",
                    "message": "Channel name required",
                }, 400
            return await _handle_subscribe(request, [channel])


def _match_channels(channels: list[str], pattern: str) -> list[str]:
    """Match channel names against a glob-like pattern.

    Supports ``*`` as a wildcard for any sequence of characters.
    The pattern is matched against the full channel name.

    Args:
        channels: List of channel names to match.
        pattern: Glob pattern (e.g. ``"test:*"``, ``"*"``)

    Returns:
        List of matching channel names.
    """
    import fnmatch

    return [ch for ch in channels if fnmatch.fnmatch(ch, pattern)]


async def _require_acl_admin(bus: Bus, channel: str) -> tuple[dict, int] | None:
    """Check that the current user has admin access on *channel*.

    Returns an error tuple (dict, status) if denied, or None if allowed.
    Supertokens and no-auth mode always pass.
    """
    auth_result = _auth_result_var.get()
    if isinstance(auth_result, str) and not await asyncio.to_thread(
        bus.check_access, channel, auth_result, "admin"
    ):
        return {
            "error": "Forbidden",
            "message": f"Agent '{auth_result}' lacks admin permission on '{channel}'",
        }, 403
    return None


def _check_subscribe_access(auth_result: Any, ch_list: list[str]) -> tuple[dict, int] | None:
    """Validate channel access for scoped tokens. Returns error tuple or None.

    Public channels (and ``_system:`` channels) are open to all
    authenticated agents. Private channels (DMs, notebook, memory)
    require the agent to appear in the ``:``-delimited segments.
    """
    if not isinstance(auth_result, str):
        return None
    forbidden = [
        ch for ch in ch_list if _is_private_channel(ch) and auth_result not in ch.split(":")
    ]
    if forbidden:
        return {
            "error": "Forbidden",
            "message": f"Token for '{auth_result}' cannot subscribe to: {', '.join(forbidden)}",
        }, 403
    return None


def _setup_sse_subscriptions(
    bus: Bus, ch_list: list[str], auth_result: Any
) -> tuple[asyncio.Queue[str | None], list[str], threading.Lock, list[int]]:
    """Subscribe to bus channels and return (queue, subscription_ids, drop_lock, drop_counter).

    Bus callbacks are sync and may run on any thread (the publisher's
    thread). We use ``loop.call_soon_threadsafe`` to safely enqueue
    data into the asyncio.Queue from non-event-loop threads.

    Returns a ``[dropped_count]`` list (mutable single-element) and
    a lock protecting it, so the async generator can report and reset
    the counter.
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)
    sub_ids: list[str] = []
    drop_lock = threading.Lock()
    drop_counter: list[int] = [0]  # mutable single-element for cross-thread access

    for ch in ch_list:

        def _make_callback(target_ch: str) -> Any:
            def _safe_enqueue(data: str) -> None:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    # Backpressure: drop oldest, enqueue new event
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait(data)
                    with drop_lock:
                        drop_counter[0] += 1

            def _cb(msg: Message) -> None:
                if isinstance(auth_result, str) and not _agent_involved(
                    auth_result, msg.channel, msg.sender
                ):
                    return
                data = json.dumps(
                    {"channel": target_ch, "message": _msg_to_dict(msg)}, ensure_ascii=False
                )
                loop.call_soon_threadsafe(_safe_enqueue, data)

            return _cb

        sid = bus.subscribe(ch, _make_callback(ch))
        sub_ids.append(sid)

    return q, sub_ids, drop_lock, drop_counter


# Max messages to replay per channel on reconnect.  For a client
# subscribed to N channels the total replayed events can be up to
# _SSE_REPLAY_LIMIT × N.
_SSE_REPLAY_LIMIT = 500


def _format_sse_event(data: str, event_id: str) -> str:
    """Format a single SSE event with optional id: and data: fields.

    If *event_id* is falsy the ``id:`` line is omitted entirely to
    avoid resetting the client's ``lastEventId`` to the empty string
    (which would break cursor-based resume on reconnect).
    """
    if event_id:
        return f"id: {event_id}\ndata: {data}\n\n"
    return f"data: {data}\n\n"


def _drain_drop_counter(
    drop_lock: threading.Lock | None,
    drop_counter: list[int] | None,
) -> str:
    """Return an SSE warning comment if events were dropped, else empty string."""
    if drop_lock is None or drop_counter is None:
        return ""
    with drop_lock:
        dropped = drop_counter[0]
        drop_counter[0] = 0
    if dropped:
        return f": warning: {dropped} event(s) dropped (slow consumer), re-query for gaps\n\n"
    return ""


async def _sse_event_generator(
    q: asyncio.Queue[str | None],
    sub_ids: list[str],
    bus: Bus,
    ch_list: list[str],
    auth_result: Any,
    last_event_id: str | None,
    drop_lock: threading.Lock | None = None,
    drop_counter: list[int] | None = None,
) -> Any:
    """Async generator that yields SSE events from the queue.

    If *last_event_id* is set (via ``Last-Event-ID`` header), replays
    missed messages from the bus before switching to live streaming.
    Each event includes an ``id:`` field (the message ID) so the
    W3C SSE client can resume on reconnect.

    When events are dropped due to slow consumption, a warning SSE
    comment is emitted so the client knows to re-query for any gaps.
    """
    try:
        yield ": connected\n\n"

        # ── Replay missed messages on reconnect ──────────────────
        if last_event_id:
            for ch in ch_list:
                missed = await asyncio.to_thread(
                    bus.query, ch, after=last_event_id, limit=_SSE_REPLAY_LIMIT
                )
                if isinstance(auth_result, str):
                    missed = [
                        m for m in missed if _agent_involved(auth_result, m.channel, m.sender)
                    ]
                for m in missed:
                    data = json.dumps(
                        {"channel": ch, "message": _msg_to_dict(m)},
                        ensure_ascii=False,
                    )
                    yield _format_sse_event(data, m.id)

        # NOTE: Subscriptions are active during replay, so a message
        # published in the replay window can appear in both the replay
        # results and the live queue (duplicate).  SSE clients that track
        # ``lastEventId`` deduplicate naturally; for others the window
        # is small and payloads are idempotent.

        # ── Live stream ──────────────────────────────────────────
        while True:
            try:
                data = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if data is None:
                break
            # Extract message id from the JSON for the SSE id: field
            try:
                parsed = json.loads(data)
                event_id = parsed.get("message", {}).get("id", "")
            except (json.JSONDecodeError, AttributeError):
                event_id = ""
            yield _format_sse_event(data, event_id)

            # Notify client about dropped events (slow consumer)
            warning = _drain_drop_counter(drop_lock, drop_counter)
            if warning:
                yield warning
    finally:
        for sid in sub_ids:
            bus.unsubscribe(sid)
