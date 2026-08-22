"""HTTP frontend for piazza — REST API + SSE push.

Exposes the Bus over HTTP/HTTPS so remote PiazzaClient instances can
publish, query, and receive real-time notifications via SSE.

Uses vendored zerodep ``httpserver`` — async, decorator-based routing,
StreamingResponse for SSE.

API Endpoints:
    POST /v1/publish         — publish a message
    GET  /v1/query           — query messages from a channel
    GET  /v1/channels        — list channels
    GET  /v1/subscribe       — SSE stream for real-time notifications
    GET  /v1/auth/check      — validate agent credentials
    GET  /v1/registry/lookup — look up agent registration
    GET  /health             — health check

Authentication:
    When a TokenStore is configured, all /v1/* endpoints (except
    /health and /v1/auth/check) require ``Authorization: Bearer pzt-...``.
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
from typing import TYPE_CHECKING, Any

from piazza._vendor.httpserver import App, JSONResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from piazza._vendor.httpserver import Request
    from piazza.bus import Bus
    from piazza.token_store import TokenStore
    from piazza.types import Message

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
    auth_result: Any, sender: str, channel: str, token_store: TokenStore | None = None
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
        # Allow: _system:agents (presence announce from own agent)
        # Allow: _system:cursors:{agent_id} (own cursor persistence)
        # Allow: _system:registry (self-registration)
        allowed_system = (
            "_system:agents",
            f"_system:cursors:{auth_result}",
            "_system:registry",
        )
        if channel not in allowed_system:
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


def _parse_query_params(request: Any, max_limit: int) -> dict:
    """Extract and validate query parameters (channel, after, limit).

    Returns dict with 'channel', 'after', 'limit' on success,
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

    return {"channel": channel, "after": after, "limit": limit, "msg_type": msg_type}


async def _handle_registry_lookup(request: Any, bus: Any) -> dict | tuple:
    """Handle /v1/registry/lookup requests."""
    agent_id = (request.query_params.get("agent_id") or [None])[0]
    if not agent_id:
        return {
            "error": "Bad Request",
            "message": "Query param 'agent_id' required",
        }, 400

    msgs = await asyncio.to_thread(bus.poll, "_system:registry", limit=1000)
    for m in reversed(msgs):
        if m.sender == agent_id and m.msg_type == "register":
            return {"found": True, "agent_id": agent_id, "metadata": m.metadata}
    return {"found": False, "agent_id": agent_id}


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
    return {
        "id": m.id,
        "channel": m.channel,
        "sender": m.sender,
        "msg_type": m.msg_type,
        "payload": m.payload,
        "timestamp": m.timestamp,
        "metadata": m.metadata,
    }


class HttpFrontend:
    """HTTP frontend — REST API + SSE push notifications.

    Exposes a Bus over HTTP so remote PiazzaClient instances
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
        self._setup_queue_routes()
        self._setup_presence_routes()
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
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
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
                    headers={"WWW-Authenticate": 'Bearer realm="piazza"'},
                )

            parts = auth_header.split(" ", 1)
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return JSONResponse(
                    {"error": "Unauthorized", "message": "Expected 'Bearer <token>'"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="piazza"'},
                )

            result = await asyncio.to_thread(token_store.validate, parts[1])
            if result is False:
                return JSONResponse(
                    {"error": "Unauthorized", "message": "Invalid or expired token"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="piazza"'},
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
                auth_result, data["sender"], data["channel"], token_store=self._token_store
            )
            if auth_error:
                return auth_error

            msg_id = await asyncio.to_thread(
                bus.publish,
                channel=data["channel"],
                sender=data["sender"],
                msg_type=data["msg_type"],
                payload=data["payload"],
                metadata=data.get("metadata"),
                queue=bool(data.get("queue")),
            )
            return {"message_id": msg_id}

        @self._app.get("/v1/query")
        async def query(request: Request) -> dict | tuple:
            qp = _parse_query_params(request, max_query_limit)
            if "error" in qp:
                status = qp.pop("_status", 400)
                return qp, status
            channel, after, limit = qp["channel"], qp["after"], qp["limit"]

            msgs = await asyncio.to_thread(
                bus.poll, channel, after=after, limit=limit, msg_type=qp.get("msg_type")
            )

            auth_result = _auth_result_var.get()
            if isinstance(auth_result, str):
                msgs = [m for m in msgs if _agent_involved(auth_result, m.channel, m.sender)]

            return {
                "messages": [_msg_to_dict(m) for m in msgs],
                "count": len(msgs),
            }

        @self._app.get("/v1/channels")
        async def channels(request: Request) -> dict:
            all_channels = await asyncio.to_thread(bus.channels)
            auth_result = _auth_result_var.get()
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

    def _setup_queue_routes(self) -> None:
        """Register queue (claim/ack) routes."""
        bus = self._bus
        assert bus is not None

        @self._app.post("/v1/claim")
        async def claim(request: Request) -> dict | tuple:
            data, err = _parse_queue_body(request, "channel")
            if err:
                return err
            claimed_by, err = _resolve_claimed_by(data)
            if err:
                return err

            lease = int(data.get("lease_seconds", 300))
            result = await asyncio.to_thread(
                bus.claim, data["channel"], claimed_by, lease_seconds=lease
            )
            if result is None:
                return {"claimed": False}
            return {"claimed": True, "result": _claim_result_to_dict(result)}

        @self._app.post("/v1/ack")
        async def ack(request: Request) -> dict | tuple:
            data, err = _parse_queue_body(request, "message_id")
            if err:
                return err
            claimed_by, err = _resolve_claimed_by(data)
            if err:
                return err

            result = await asyncio.to_thread(bus.ack, data["message_id"], claimed_by)
            if result is None:
                return {"acked": False}
            return {"acked": True, "result": _claim_result_to_dict(result)}

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

    def _setup_sse_route(self) -> None:
        """Register the SSE subscribe route."""
        bus = self._bus
        assert bus is not None
        cors = self._cors_origin

        @self._app.get("/v1/subscribe")
        async def subscribe(request: Request) -> StreamingResponse | tuple:
            """SSE endpoint with validate-once-at-connect auth."""
            ch_list = request.query_params.get("channel", [])
            if not ch_list:
                return {
                    "error": "Bad Request",
                    "message": "At least one 'channel' param required",
                }, 400

            auth_result = _auth_result_var.get()
            error = _check_subscribe_access(auth_result, ch_list)
            if error:
                return error

            q, sub_ids = _setup_sse_subscriptions(bus, ch_list, auth_result)

            return StreamingResponse(
                _sse_event_generator(q, sub_ids, bus),
                content_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Access-Control-Allow-Origin": cors,
                },
            )


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
) -> tuple[asyncio.Queue[str | None], list[str]]:
    """Subscribe to bus channels and return (queue, subscription_ids).

    Bus callbacks are sync and may run on any thread (the publisher's
    thread). We use ``loop.call_soon_threadsafe`` to safely enqueue
    data into the asyncio.Queue from non-event-loop threads.
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)
    sub_ids: list[str] = []

    for ch in ch_list:

        def _make_callback(target_ch: str) -> Any:
            def _safe_enqueue(data: str) -> None:
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(data)

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

    return q, sub_ids


async def _sse_event_generator(q: asyncio.Queue[str | None], sub_ids: list[str], bus: Bus) -> Any:
    """Async generator that yields SSE events from the queue."""
    try:
        yield ": connected\n\n"
        while True:
            try:
                data = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if data is None:
                break
            yield f"data: {data}\n\n"
    finally:
        for sid in sub_ids:
            bus.unsubscribe(sid)
