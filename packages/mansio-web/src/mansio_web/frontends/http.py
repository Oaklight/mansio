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
    GET  /v1/registry/lookup — check if an agent is known (presence-based)
    POST /v1/admin/compact   — compact a channel (admin only)
    GET  /health             — health check

Authentication:
    All /v1/* endpoints (except /health and /v1/auth/check) require
    ``Authorization: Bearer mst-...``. The token is validated server-side,
    and the sender field in publish requests must match the token's user_id.
    Supertokens (user_id=NULL) bypass sender checks.

    A frontend with no TokenStore cannot validate anything, so it refuses
    every authenticated endpoint. Serving an open API is possible but never
    implicit: it requires ``allow_unauthenticated=True``, which logs a
    warning at startup.

Presence:
    Any authenticated request, and any live SSE subscription, refreshes
    that user's ``last_seen``, so ``users()`` reflects who is actually
    active without the client having to remember to heartbeat. Writes are
    throttled per user. ``POST /v1/presence/heartbeat`` remains for clients
    that are alive but idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from mansio_core.authz import (
    DM_NAME_EXPECTED,
    DM_PREFIX,
    ChannelAuthorizer,
    dm_channel_error,
    dm_participants,
)
from mansio_core.authz import user_involved as _user_involved
from mansio_core.contracts import (
    ACLAddEntry,
    ACLAddResult,
    ACLRemoveResult,
    ACLResult,
    ACLSetResult,
    AdminCleanupResult,
    AdminCompactResult,
    AuthCheckResult,
    ChannelResult,
    ChannelsResult,
    ContractError,
    DeleteChannelResult,
    DeleteMessageResult,
    ErrorResult,
    HealthResult,
    PresenceAgentResult,
    PresenceHeartbeatResult,
    PresenceResult,
    PublishResult,
    QueryResult,
    QueueAckResult,
    QueueClaimResult,
    QueueStatusResult,
    RegistryLookupResult,
    StreamEvent,
    parse_acl_add,
    parse_acl_set,
    parse_admin_cleanup,
    parse_admin_compact,
    parse_channel_create,
    parse_channels,
    parse_heartbeat,
    parse_presence_list,
    parse_publish,
    parse_query,
    parse_queue_ack,
    parse_queue_claim,
    parse_queue_status,
    parse_registry_lookup,
    parse_stream_event,
    stream_event_to_json,
)

from mansio_web._vendor.httpserver import App, JSONResponse, Response, StreamingResponse
from mansio_web._vendor.structlog import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from mansio_core.bus import Bus
    from mansio_core.token_store import TokenStore
    from mansio_core.types import Message

    from mansio_web._vendor.httpserver import Request

logger = get_logger(__name__)

# Paths that skip token auth
_PUBLIC_PATHS = frozenset({"/health", "/v1/auth/check"})

# Returned for every authenticated endpoint when the frontend has no way to
# authenticate anyone. Both causes are the same failure from a caller's side —
# no token can be presented that would work — so they answer alike.
_NO_TOKEN_STORE_MESSAGE = (
    "This server has no token store, so it cannot authenticate anyone and "
    "refuses all authenticated endpoints. Configure a TokenStore, or pass "
    "allow_unauthenticated=True to HttpFrontend to serve an open API on purpose."
)
_NO_TOKENS_MESSAGE = (
    "No API tokens configured. Create a token via the admin panel before connecting."
)

# Shortest interval between two presence writes for the same user. last_seen
# only has to stay fresh enough that users() still counts the user as online,
# so this sits well inside that presence timeout while holding even a busy
# client to one write per interval.
_PRESENCE_REFRESH_SECONDS = 30.0


@dataclass
class _SSESession:
    """Bundles per-connection SSE state to reduce parameter sprawl."""

    queue: asyncio.Queue[str | None]
    sub_ids: list[str]
    bus: Any
    channels: list[str]
    auth_result: Any
    last_event_id: str | None = None
    drop_lock: threading.Lock | None = None
    drop_counter: list[int] | None = None
    # Opt-in: drop messages whose sender is the authenticated caller, for
    # live delivery and Last-Event-ID replay alike.  No effect when the
    # connection has no bound user (supertoken, no-auth).
    exclude_own: bool = False
    # Called as the stream stays alive so an idle subscriber still counts
    # as present. None when the connection has no user identity.
    on_activity: Callable[[], None] | None = None


# Channel naming rules:
# - User channels: 3-64 chars, must contain a letter, start with [a-z],
#   end with [a-z0-9], no consecutive special chars
# - System/auto channels (_system:, dm:, notebook:, memory:, broadcast:)
#   are exempt from user rules — validated separately
# User channels: no colons (reserved for system prefixes), Unicode letters OK
# End anchor [^\W_] = letters/digits (no trailing underscore)
_USER_CHANNEL_RE = re.compile(r"^(?=[^\W\d_])[\w.-]{1,63}[^\W_]\Z")
_NO_CONSECUTIVE_SPECIALS = re.compile(r"[._-]{2}")
_RESERVED_PREFIXES = ("_system:", "dm:", "notebook:", "memory:", "broadcast:")
_SYSTEM_CHANNEL_RE = re.compile(r"^[\w_][\w:.-]{1,126}[\w]\Z")


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _invalid_dm_name(channel: str) -> bool:
    """True if *channel* uses the ``dm:`` prefix but is not a valid DM name.

    Combines the structural rule shared with the authorization predicates
    (``mansio_core.authz``) with the character rule of the generic system channel
    pattern, so a name accepted here can always be published to later.
    """
    return channel.startswith(DM_PREFIX) and (
        dm_channel_error(channel) is not None or not _SYSTEM_CHANNEL_RE.match(channel)
    )


def _require_valid_dm_channel(channel: str) -> None:
    """Raise a contract error for a malformed ``dm:`` channel name."""
    if _invalid_dm_name(channel):
        raise ContractError(DM_NAME_EXPECTED, field="channel")


def _validate_channel_name(
    channel: str, token_store: TokenStore | None = None
) -> tuple[dict, int] | None:
    """Validate channel name. Returns error tuple or None if OK."""
    if _invalid_dm_name(channel):
        return _error("Bad Request", DM_NAME_EXPECTED, 400)

    is_reserved = any(channel.startswith(p) for p in _RESERVED_PREFIXES)

    if is_reserved:
        if not _SYSTEM_CHANNEL_RE.match(channel):
            return _error("Bad Request", "Invalid system channel name", 400)
        return None

    # Check for custom registered prefix (channel contains colon)
    if ":" in channel:
        prefix = channel.split(":")[0]
        if token_store and token_store.is_registered_prefix(prefix):
            if len(channel) < 3 or len(channel) > 64 or channel != channel.lower():
                return _error("Bad Request", "Invalid prefixed channel name", 400)
            return None
        return _error(
            "Bad Request",
            f"Prefix '{prefix}:' is not registered. Register custom prefixes via admin panel.",
            400,
        )

    # Plain user channel — no colons
    if len(channel) < 3 or not _USER_CHANNEL_RE.match(channel):
        return _error(
            "Bad Request",
            "Channel name must be 3-64 chars, start with letter, end with letter/digit, "
            "lowercase, no colons (use registered prefixes for namespaced channels)",
            400,
        )
    if not any(c.isalpha() for c in channel):
        return _error("Bad Request", "Channel name must contain at least one letter", 400)
    if channel != channel.lower():
        return _error("Bad Request", "Channel name must be lowercase", 400)
    if _NO_CONSECUTIVE_SPECIALS.search(channel):
        return _error(
            "Bad Request", "Channel name cannot contain consecutive special characters", 400
        )

    return None


def _validate_and_auth_publish(
    auth_result: Any,
    sender: str,
    channel: str,
    msg_type: str = "",
    token_store: TokenStore | None = None,
    authz: ChannelAuthorizer | None = None,
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
        return _error(
            "Forbidden", f"Token bound to '{auth_result}', cannot publish as '{sender}'", 403
        )

    denial = authz.publish_denial(auth_result, channel, msg_type) if authz else None
    if denial:
        return _error("Forbidden", denial, 403)

    return None


def _error_body(error: str, message: str) -> dict[str, str]:
    """Return one of the shared error documents."""
    return ErrorResult(error=error, message=message).to_dict()


def _error(error: str, message: str, status: int) -> tuple[dict[str, str], int]:
    """Return an error document with its HTTP status."""
    return _error_body(error, message), status


def _bad_request(err: ContractError) -> tuple[dict[str, str], int]:
    """Return a contract error as an HTTP 400 response."""
    return _error_body("Bad Request", err.message), 400


def _json_body(request: Any, message: str = "Request body must be valid JSON") -> Any:
    """Decode the request body, mapping JSON failures to a contract error."""
    try:
        return request.json()
    except Exception as exc:
        raise ContractError(message) from exc


async def _dm_unregistered_warning(bus: Any, channel: str, sender: str) -> str | None:
    """Return a warning string if a DM targets an unregistered agent."""
    participants = dm_participants(channel)
    if participants is None:
        return None
    other = participants[0] if participants[1] == sender else participants[1]
    try:
        status = await asyncio.to_thread(bus.user_status, other)
    except NotImplementedError:
        return None
    if status is None:
        return f"target agent '{other}' is not registered; message stored but may never be read"
    return None


async def _handle_queue_status(request: Any, bus: Any, authz: ChannelAuthorizer) -> dict | tuple:
    """Handle /v1/queue/status requests."""
    try:
        status = parse_queue_status(request.query_params)
    except ContractError as err:
        return _bad_request(err)
    message_id = status.message_id

    auth_result = request.state.auth_result
    if isinstance(auth_result, str):
        message = await asyncio.to_thread(bus.get_message, message_id)
        if message is not None and not authz.can_read(auth_result, message.channel):
            # Same body as a message without queue state, so the answer does
            # not confirm that a private message exists.
            return _error("Not Found", "No queue status for this message", 404)

    result = await asyncio.to_thread(bus.queue_status, message_id)
    if result is None:
        return _error("Not Found", "No queue status for this message", 404)
    return QueueStatusResult(found=True, status=result).to_dict()


def _resolve_claimed_by(auth_result: Any, claimed_by: str | None) -> tuple[str, tuple | None]:
    """Resolve claimed_by from the auth context or the parsed request body."""
    if auth_result is False:
        return "", (_error("Unauthorized", "Valid token required", 401))
    if isinstance(auth_result, str):
        return auth_result, None
    if not claimed_by:
        return "", (_error("Bad Request", "Missing 'claimed_by'", 400))
    return claimed_by, None


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
        token_store: TokenStore used to authenticate API requests. Without
            one the frontend rejects every authenticated endpoint.
        allow_unauthenticated: Serve the API with no authentication at all,
            treating every caller as a supertoken that may act as any sender.
            For local development and tests only. Warns on startup. Cannot be
            combined with token_store.

    Raises:
        ValueError: If allow_unauthenticated is set alongside a token_store.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8742,
        cors_origin: str = "*",
        max_body_bytes: int = 1_048_576,
        max_query_limit: int = 10_000,
        token_store: TokenStore | None = None,
        allow_unauthenticated: bool = False,
    ) -> None:
        if allow_unauthenticated and token_store is not None:
            raise ValueError(
                "allow_unauthenticated cannot be combined with a token_store — "
                "pass one or the other so the intended auth posture is unambiguous"
            )
        self._host = host
        self._port = port
        self._cors_origin = cors_origin
        self._max_query_limit = max_query_limit
        self._token_store = token_store
        self._allow_unauthenticated = allow_unauthenticated
        self._bus: Bus | None = None
        self._authz: ChannelAuthorizer | None = None
        self._app = App(max_body_size=max_body_bytes)
        # user_id → monotonic timestamp of its last presence write. Only
        # touched from the event loop, so it needs no lock.
        self._presence_written: dict[str, float] = {}
        self._presence_tasks: set[asyncio.Task] = set()
        self._presence_supported = True

    # ── Presence ──────────────────────────────────────────────────

    def _note_activity(self, user_id: str) -> None:
        """Record that *user_id* is active, at most once per refresh interval.

        Presence is derived from real API activity rather than from an
        explicit heartbeat the client has to remember to send. Callers are
        request handlers, so the write is dispatched to a background task:
        a caller's publish must not wait on, or fail because of, a
        bookkeeping write.
        """
        if not self._presence_supported:
            return
        now = time.monotonic()
        written = self._presence_written.get(user_id)
        if written is not None and now - written < _PRESENCE_REFRESH_SECONDS:
            return
        self._presence_written[user_id] = now
        task = asyncio.create_task(self._write_presence(user_id))
        self._presence_tasks.add(task)
        task.add_done_callback(self._presence_tasks.discard)

    async def _write_presence(self, user_id: str) -> None:
        """Persist *user_id*'s activity as presence."""
        bus = self._bus
        if bus is None:
            return
        try:
            await asyncio.to_thread(bus.heartbeat, user_id)
        except NotImplementedError:
            # This backend has no presence store and will not grow one while
            # running, so stop attempting the write rather than repeating it
            # for every user on every interval.
            self._presence_supported = False
            logger.info("Backend has no presence store — activity will not update last_seen")
        except Exception as exc:
            # Recovery: drop the throttle entry so the next request retries.
            # The request that triggered this has already been answered and
            # must not be affected by a failed bookkeeping write.
            self._presence_written.pop(user_id, None)
            logger.warning(
                "Presence update failed",
                user_id=user_id,
                error=f"{type(exc).__name__}: {exc}",
            )

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
        self._authz = ChannelAuthorizer(bus)
        self._setup_middleware()
        self._setup_api_routes()
        self._setup_deletion_routes()
        self._setup_registry_route()
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
        if self._allow_unauthenticated:
            logger.warning(
                "Serving an unauthenticated API — every caller may act as any sender",
                host=self._host,
                port=self._port,
                override="allow_unauthenticated=True",
            )
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
        if self._token_store:
            auth = "auth"
        elif self._allow_unauthenticated:
            auth = "no-auth"
        else:
            auth = "auth-unavailable"
        return f"HttpFrontend({addr}, {status}, {auth})"

    # ── Middleware ─────────────────────────────────────────────────

    def _setup_middleware(self) -> None:
        """Register auth and CORS middleware."""
        cors = self._cors_origin
        token_store = self._token_store
        allow_unauthenticated = self._allow_unauthenticated

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

            if token_store is None:
                if allow_unauthenticated:
                    request.state.auth_result = True
                    return None
                return JSONResponse(
                    _error_body("Forbidden", _NO_TOKEN_STORE_MESSAGE),
                    status_code=403,
                )

            if not await asyncio.to_thread(token_store.has_tokens):
                return JSONResponse(
                    _error_body("Forbidden", _NO_TOKENS_MESSAGE),
                    status_code=403,
                )

            auth_header = request.headers.get("authorization", "")
            if not auth_header:
                return JSONResponse(
                    _error_body("Unauthorized", "Missing Authorization header"),
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mansio"'},
                )

            parts = auth_header.split(" ", 1)
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return JSONResponse(
                    _error_body("Unauthorized", "Expected 'Bearer <token>'"),
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mansio"'},
                )

            result = await asyncio.to_thread(token_store.validate, parts[1])
            if result is False:
                return JSONResponse(
                    _error_body("Unauthorized", "Invalid or expired token"),
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mansio"'},
                )

            # result is str (user_id) or None (supertoken)
            request.state.auth_result = result
            if isinstance(result, str):
                self._note_activity(result)
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
        authz = self._authz
        assert authz is not None
        max_query_limit = self._max_query_limit

        @self._app.get("/health")
        async def health(request: Request) -> dict:
            return HealthResult().to_dict()

        @self._app.post("/v1/publish")
        async def publish(request: Request) -> dict | tuple:
            try:
                payload = parse_publish(_json_body(request))
            except ContractError as err:
                return _bad_request(err)

            auth_result = request.state.auth_result
            auth_error = _validate_and_auth_publish(
                auth_result,
                payload.sender,
                payload.channel,
                msg_type=payload.msg_type,
                token_store=self._token_store,
                authz=self._authz,
            )
            if auth_error:
                return auth_error

            try:
                msg_id = await asyncio.to_thread(
                    bus.publish,
                    channel=payload.channel,
                    sender=payload.sender,
                    msg_type=payload.msg_type,
                    payload=payload.payload,
                    metadata=payload.metadata,
                    queue=payload.queue,
                    parent_id=payload.parent_id,
                    intent=payload.intent,
                )
            except ValueError as e:
                status = 404 if "not found" in str(e) else 400
                error = "Not Found" if status == 404 else "Bad Request"
                return _error(error, str(e), status)
            warning = await _dm_unregistered_warning(bus, payload.channel, payload.sender)
            return PublishResult(message_id=msg_id, warning=warning).to_dict()

        @self._app.get("/v1/query")
        async def query(request: Request) -> dict | tuple:
            try:
                qp = parse_query(
                    request.query_params,
                    max_limit=max_query_limit,
                    validate_channel=_require_valid_dm_channel,
                )
            except ContractError as err:
                return _bad_request(err)

            # Early access check: scoped tokens cannot query private
            # channels they don't belong to.  This also prevents leaking
            # the total message count (issue #251).
            auth_result = request.state.auth_result
            if isinstance(auth_result, str) and not authz.can_read(auth_result, qp.channel):
                return _error(
                    "Forbidden",
                    f"Token for '{auth_result}' cannot query channel '{qp.channel}'",
                    403,
                )

            msgs = await asyncio.to_thread(
                bus.query,
                qp.channel,
                after=qp.after,
                limit=qp.limit,
                msg_type=qp.msg_type,
                order=qp.order,
                thread_id=qp.thread_id,
                intent=qp.intent,
                offset=qp.offset,
            )

            msgs = _filter_visible_messages(auth_result, msgs)

            total = await asyncio.to_thread(bus.message_count, qp.channel)
            has_more = len(msgs) == qp.limit

            return QueryResult(
                messages=msgs,
                count=len(msgs),
                total=total,
                offset=qp.offset,
                has_more=has_more,
            ).to_dict()

        def _visible_channels_detail(user_id: str) -> list[dict]:
            """List channel detail for one user, filtered in a single thread hop."""
            detail = bus.channels_detail()
            allowed = authz.can_list_channels(user_id, [ch["name"] for ch in detail])
            return [ch for ch in detail if allowed[ch["name"]]]

        def _visible_channel_names(user_id: str) -> list[str]:
            """List channel names for one user, filtered in a single thread hop."""
            names = cast(list[str], bus.channels(detail=False))
            allowed = authz.can_list_channels(user_id, names)
            return [name for name in names if allowed[name]]

        @self._app.get("/v1/channels")
        async def channels(request: Request) -> dict | tuple:
            want_detail = parse_channels(request.query_params).detail

            auth_result = request.state.auth_result

            if want_detail:
                if isinstance(auth_result, str):
                    all_detail = await asyncio.to_thread(_visible_channels_detail, auth_result)
                else:
                    all_detail = await asyncio.to_thread(bus.channels_detail)
                return ChannelsResult(channels=all_detail).to_dict()

            if isinstance(auth_result, str):
                all_channels = await asyncio.to_thread(_visible_channel_names, auth_result)
            else:
                all_channels = cast(list[str], await asyncio.to_thread(bus.channels, detail=False))
            return ChannelsResult(channels=all_channels).to_dict()

        self._setup_auth_check_route()

    def _setup_auth_check_route(self) -> None:
        """Register /v1/auth/check route."""

        @self._app.get("/v1/auth/check")
        async def auth_check(request: Request) -> dict:
            ts = self._token_store
            has_tokens = bool(ts and await asyncio.to_thread(ts.has_tokens))
            # Only the explicit opt-out disables auth. A frontend that merely
            # lacks a token store still requires a token it can never accept,
            # which is what the authenticated endpoints report too.
            return AuthCheckResult(
                auth_mode="disabled" if self._allow_unauthenticated else "required",
                has_tokens=has_tokens,
            ).to_dict()

    def _setup_registry_route(self) -> None:
        """Register registry lookup route."""
        bus = self._bus
        assert bus is not None

        @self._app.get("/v1/registry/lookup")
        async def registry_lookup(request: Request) -> dict | tuple:
            try:
                query = parse_registry_lookup(request.query_params)
            except ContractError as err:
                return _bad_request(err)
            user_id = query.user_id
            token_store = self._token_store
            if token_store is not None and await asyncio.to_thread(
                token_store.user_exists, user_id
            ):
                return RegistryLookupResult(found=True, user_id=user_id).to_dict()
            return RegistryLookupResult(found=False, user_id=user_id).to_dict()

    def _setup_deletion_routes(self) -> None:
        """Register channel and message deletion routes."""
        self._setup_delete_channel_route()
        self._setup_delete_message_route()
        self._setup_admin_cleanup_route()
        self._setup_admin_compact_route()

    def _setup_delete_channel_route(self) -> None:
        """Register DELETE /v1/channels/<channel_name>."""
        bus = self._bus
        assert bus is not None
        authz = self._authz
        assert authz is not None

        @self._app.delete("/v1/channels/<channel_name>")
        async def delete_channel(request: Request, channel_name: str = "") -> dict | tuple:
            if not channel_name:
                return _error("Bad Request", "Channel name required", 400)

            auth_result = request.state.auth_result
            if isinstance(auth_result, str):
                denial = authz.delete_channel_denial(auth_result, channel_name)
                if denial:
                    return _error("Forbidden", denial, 403)

            try:
                count = await asyncio.to_thread(bus.delete_channel, channel_name)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support deletion", 501)

            if count == 0:
                return _error(
                    "Not Found", f"Channel '{channel_name}' not found or already empty", 404
                )

            return DeleteChannelResult(deleted=count, channel=channel_name).to_dict()

    def _setup_delete_message_route(self) -> None:
        """Register DELETE /v1/messages/<message_id>."""
        bus = self._bus
        assert bus is not None
        authz = self._authz
        assert authz is not None

        @self._app.delete("/v1/messages/<message_id>")
        async def delete_message(request: Request, message_id: str = "") -> dict | tuple:
            if not message_id:
                return _error("Bad Request", "Message ID required", 400)

            auth_result = request.state.auth_result

            # For scoped tokens, verify the message belongs to the agent
            if isinstance(auth_result, str):
                found_msg = await asyncio.to_thread(bus.get_message, message_id)

                if found_msg is None:
                    return _error("Not Found", f"Message '{message_id}' not found", 404)

                if not authz.can_delete_message(auth_result, found_msg):
                    return _error(
                        "Forbidden",
                        f"Token for '{auth_result}' cannot delete messages "
                        f"from sender '{found_msg.sender}'",
                        403,
                    )

            try:
                deleted = await asyncio.to_thread(bus.delete_message, message_id)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support deletion", 501)

            if not deleted:
                return _error("Not Found", f"Message '{message_id}' not found", 404)

            return DeleteMessageResult(deleted=True, message_id=message_id).to_dict()

    def _setup_admin_cleanup_route(self) -> None:
        """Register POST /v1/admin/channels/cleanup."""
        bus = self._bus
        assert bus is not None

        @self._app.post("/v1/admin/channels/cleanup")
        async def admin_cleanup(request: Request) -> dict | tuple:
            auth_result = request.state.auth_result

            # Admin only: supertoken (None) or no-auth mode (True)
            if isinstance(auth_result, str):
                return _error("Forbidden", "Admin endpoints require a supertoken", 403)

            try:
                cleanup = parse_admin_cleanup(_json_body(request))
            except ContractError as err:
                return _bad_request(err)

            pattern = cleanup.pattern
            older_than = cleanup.older_than

            all_channels = cast(list[str], await asyncio.to_thread(bus.channels, detail=False))
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
                return _error("Not Implemented", "Backend does not support deletion", 501)

            return AdminCleanupResult(
                channels_deleted=len(channels_deleted),
                messages_deleted=total_deleted,
                channels=channels_deleted,
            ).to_dict()

    def _setup_admin_compact_route(self) -> None:
        """Register POST /v1/admin/compact."""
        bus = self._bus
        assert bus is not None

        @self._app.post("/v1/admin/compact")
        async def admin_compact(request: Request) -> dict | tuple:
            auth_result = request.state.auth_result

            # Admin only: supertoken (None) or no-auth mode (True)
            if isinstance(auth_result, str):
                return _error("Forbidden", "Admin endpoints require a supertoken", 403)

            try:
                compact = parse_admin_compact(_json_body(request))
            except ContractError as err:
                return _bad_request(err)

            channel = compact.channel
            max_messages = compact.max_messages
            keep_latest_per_sender = compact.keep_latest_per_sender

            try:
                removed = await asyncio.to_thread(
                    bus.compact,
                    channel,
                    max_messages=max_messages,
                    keep_latest_per_sender=keep_latest_per_sender,
                )
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support compaction", 501)

            return AdminCompactResult(channel=channel, messages_removed=removed).to_dict()

    def _setup_queue_routes(self) -> None:
        """Register queue (claim/ack/status) routes."""
        bus = self._bus
        assert bus is not None
        authz = self._authz
        assert authz is not None

        # ── Shared handlers ───────────────────────────────────────

        async def _handle_claim(request: Request) -> dict | tuple:
            try:
                claim = parse_queue_claim(_json_body(request, "Invalid JSON"))
            except ContractError as err:
                return _bad_request(err)
            auth_result = request.state.auth_result
            claimed_by, err = _resolve_claimed_by(auth_result, claim.claimed_by)
            if err:
                return err

            if isinstance(auth_result, str):
                denial = await asyncio.to_thread(authz.claim_denial, auth_result, claim.channel)
                if denial:
                    return _error("Forbidden", denial, 403)

            result = await asyncio.to_thread(
                bus.queue_claim, claim.channel, claimed_by, lease_seconds=claim.lease_seconds
            )
            if result is None:
                return QueueClaimResult(claimed=False).to_dict()
            return QueueClaimResult(claimed=True, result=result).to_dict()

        async def _handle_ack(request: Request) -> dict | tuple:
            try:
                ack = parse_queue_ack(_json_body(request, "Invalid JSON"))
            except ContractError as err:
                return _bad_request(err)
            auth_result = request.state.auth_result
            claimed_by, err = _resolve_claimed_by(auth_result, ack.claimed_by)
            if err:
                return err

            if isinstance(auth_result, str):
                message = await asyncio.to_thread(bus.get_message, ack.message_id)
                if message is not None and not authz.can_read(auth_result, message.channel):
                    # Same answer as "not claimed by you": an unauthorized
                    # caller learns nothing about the message's existence.
                    return QueueAckResult(acked=False).to_dict()

            result = await asyncio.to_thread(bus.queue_ack, ack.message_id, claimed_by)
            if result is None:
                return QueueAckResult(acked=False).to_dict()
            return QueueAckResult(acked=True, result=result).to_dict()

        # ── Canonical paths (/v1/queue/*) ─────────────────────────

        @self._app.post("/v1/queue/claim")
        async def queue_claim(request: Request) -> dict | tuple:
            return await _handle_claim(request)

        @self._app.post("/v1/queue/ack")
        async def queue_ack(request: Request) -> dict | tuple:
            return await _handle_ack(request)

        @self._app.get("/v1/queue/status")
        async def queue_status(request: Request) -> dict | tuple:
            return await _handle_queue_status(request, bus, authz)

    def _setup_presence_routes(self) -> None:
        """Register presence (heartbeat / roster) routes."""
        bus = self._bus
        assert bus is not None

        @self._app.post("/v1/presence/heartbeat")
        async def presence_heartbeat(request: Request) -> dict | tuple:
            auth_result = request.state.auth_result
            try:
                heartbeat = parse_heartbeat(_json_body(request, "JSON body required"))
            except ContractError as err:
                return _bad_request(err)
            user_id = heartbeat.user_id
            # A scoped token may only report its own user's presence.
            # Supertokens (auth_result is None) and no-auth mode (True)
            # keep acting on behalf of any user, as they do elsewhere.
            if isinstance(auth_result, str) and user_id != auth_result:
                return _error(
                    "Forbidden",
                    f"Token bound to '{auth_result}', cannot heartbeat as '{user_id}'",
                    403,
                )
            metadata = heartbeat.metadata
            try:
                await asyncio.to_thread(bus.heartbeat, user_id, metadata)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support presence", 501)
            return PresenceHeartbeatResult().to_dict()

        @self._app.get("/v1/presence")
        async def presence_list(request: Request) -> dict | tuple:
            try:
                query = parse_presence_list(request.query_params)
            except ContractError as err:
                return _bad_request(err)
            try:
                agents = await asyncio.to_thread(bus.users, query.timeout)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support presence", 501)
            return PresenceResult(agents=agents).to_dict()

        @self._app.get("/v1/presence/<user_id>")
        async def presence_agent(request: Request, user_id: str = "") -> dict | tuple:
            try:
                query = parse_presence_list(request.query_params)
            except ContractError as err:
                return _bad_request(err)
            try:
                result = await asyncio.to_thread(bus.user_status, user_id, query.timeout)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support presence", 501)
            if result is None:
                return _error("Not Found", "agent not found", 404)
            return PresenceAgentResult(
                user_id=result.user_id,
                status=result.status,
                last_seen=result.last_seen,
                metadata=result.metadata,
            ).to_dict()

    def _setup_channel_acl_routes(self) -> None:
        """Register channel metadata and ACL management routes."""
        self._setup_channel_meta_routes()
        self._setup_acl_crud_routes()

    def _setup_channel_meta_routes(self) -> None:
        """Register channel metadata routes (create, get)."""
        bus = self._bus
        assert bus is not None
        authz = self._authz
        assert authz is not None

        @self._app.post("/v1/channels")
        async def create_channel(request: Request) -> dict | tuple:
            try:
                payload = parse_channel_create(_json_body(request))
            except ContractError as err:
                return _bad_request(err)
            name = payload.name

            if name.startswith(DM_PREFIX):
                error = _validate_channel_name(name, token_store=self._token_store)
                if error:
                    return error

            auth_result = request.state.auth_result
            owner = payload.owner or ""
            if isinstance(auth_result, str):
                denial = authz.create_denial(auth_result, name)
                if denial:
                    return _error("Forbidden", denial, 403)
                owner = auth_result  # scoped tokens own what they create
            elif not owner:
                return _error("Bad Request", "'owner' is required for supertoken/no-auth", 400)

            visibility = payload.visibility

            try:
                meta = await asyncio.to_thread(
                    bus.create_channel, name, owner, visibility=visibility
                )
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support channel management", 501)
            except ValueError as exc:
                return _error("Conflict", str(exc), 409)

            return ChannelResult(channel=meta).to_dict(), 201

        @self._app.get("/v1/channels/<channel>/meta")
        async def get_channel_meta(request: Request, channel: str = "") -> dict | tuple:
            auth_result = request.state.auth_result
            if isinstance(auth_result, str) and not authz.can_read(auth_result, channel):
                # 404, not 403: a denied caller must not learn from the
                # status code that a private channel exists.  The message is
                # the same one an unknown channel returns.
                return _error("Not Found", f"Channel '{channel}' not found", 404)

            try:
                meta = await asyncio.to_thread(bus.get_channel_meta, channel)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support channel management", 501)

            if meta is None:
                return _error("Not Found", f"Channel '{channel}' not found", 404)

            return ChannelResult(channel=meta).to_dict()

    def _setup_acl_crud_routes(self) -> None:
        """Register ACL management routes (get, set, add, remove)."""
        self._setup_acl_read_route()
        self._setup_acl_write_routes()

    def _setup_acl_read_route(self) -> None:
        """Register GET /v1/channels/<channel>/acl."""
        bus = self._bus
        assert bus is not None
        authz = self._authz
        assert authz is not None

        @self._app.get("/v1/channels/<channel>/acl")
        async def get_acl(request: Request, channel: str = "") -> dict | tuple:
            auth_result = request.state.auth_result
            denied = await _require_acl_manage(auth_result, authz, channel)
            if denied:
                return denied

            try:
                entries = await asyncio.to_thread(bus.get_acl, channel)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support channel management", 501)

            return ACLResult(acl=entries).to_dict()

    def _setup_acl_write_routes(self) -> None:
        """Register PUT/POST/DELETE ACL mutation routes."""
        self._setup_acl_set_route()
        self._setup_acl_add_route()
        self._setup_acl_remove_route()

    def _setup_acl_set_route(self) -> None:
        """Register PUT /v1/channels/<channel>/acl."""
        bus = self._bus
        assert bus is not None
        authz = self._authz
        assert authz is not None

        @self._app.put("/v1/channels/<channel>/acl")
        async def set_acl(request: Request, channel: str = "") -> dict | tuple:
            auth_result = request.state.auth_result
            denied = await _require_acl_manage(auth_result, authz, channel)
            if denied:
                return denied

            try:
                payload = parse_acl_set(_json_body(request))
            except ContractError as err:
                return _bad_request(err)

            from mansio_core.types import ACLEntry

            auth_result = request.state.auth_result
            now = _now_iso()
            granted_by = auth_result if isinstance(auth_result, str) else None
            entries = [
                ACLEntry(
                    channel=channel,
                    user_id=spec.user_id,
                    permission=spec.permission,
                    granted_at=now,
                    granted_by=granted_by,
                )
                for spec in payload.entries
            ]

            try:
                if await asyncio.to_thread(bus.get_channel_meta, channel) is None:
                    return _error("Not Found", f"Channel '{channel}' not found", 404)
                await asyncio.to_thread(bus.set_acl, channel, entries)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support channel management", 501)

            return ACLSetResult(status="ok", count=len(entries)).to_dict()

    def _setup_acl_add_route(self) -> None:
        """Register POST /v1/channels/<channel>/acl."""
        bus = self._bus
        assert bus is not None
        authz = self._authz
        assert authz is not None

        @self._app.post("/v1/channels/<channel>/acl")
        async def add_acl_entry(request: Request, channel: str = "") -> dict | tuple:
            auth_result = request.state.auth_result
            denied = await _require_acl_manage(auth_result, authz, channel)
            if denied:
                return denied

            try:
                payload = parse_acl_add(_json_body(request))
            except ContractError as err:
                return _bad_request(err)
            user_id = payload.user_id
            permission = payload.permission

            from mansio_core.types import ACLEntry

            auth_result = request.state.auth_result
            now = _now_iso()
            granted_by = auth_result if isinstance(auth_result, str) else None
            entry = ACLEntry(
                channel=channel,
                user_id=user_id,
                permission=permission,
                granted_at=now,
                granted_by=granted_by,
            )

            try:
                if await asyncio.to_thread(bus.get_channel_meta, channel) is None:
                    return _error("Not Found", f"Channel '{channel}' not found", 404)
                await asyncio.to_thread(bus.add_acl_entry, entry)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support channel management", 501)

            return ACLAddResult(
                status="ok",
                entry=ACLAddEntry(
                    channel=entry.channel,
                    user_id=entry.user_id,
                    permission=entry.permission,
                ),
            ).to_dict(), 201

    def _setup_acl_remove_route(self) -> None:
        """Register DELETE /v1/channels/<channel>/acl/<user_id>."""
        bus = self._bus
        assert bus is not None
        authz = self._authz
        assert authz is not None

        @self._app.delete("/v1/channels/<channel>/acl/<user_id>")
        async def remove_acl_entry(
            request: Request, channel: str = "", user_id: str = ""
        ) -> dict | tuple:
            auth_result = request.state.auth_result
            denied = await _require_acl_manage(auth_result, authz, channel)
            if denied:
                return denied

            try:
                removed = await asyncio.to_thread(bus.remove_acl_entry, channel, user_id)
            except NotImplementedError:
                return _error("Not Implemented", "Backend does not support channel management", 501)

            if not removed:
                return _error("Not Found", f"No ACL entry for '{user_id}' on '{channel}'", 404)

            return ACLRemoveResult().to_dict()

    def _setup_sse_route(self) -> None:
        """Register the SSE subscribe routes."""
        bus = self._bus
        assert bus is not None
        authz = self._authz
        assert authz is not None
        cors = self._cors_origin

        async def _handle_subscribe(
            request: Request, ch_list: list[str]
        ) -> StreamingResponse | tuple:
            """Shared SSE handler for all subscribe endpoints."""
            if not ch_list:
                return _error("Bad Request", "At least one channel required", 400)

            for ch in ch_list:
                if _invalid_dm_name(ch):
                    return _error("Bad Request", f"{DM_NAME_EXPECTED} (got '{ch}')", 400)

            auth_result = request.state.auth_result
            if isinstance(auth_result, str):
                forbidden = [ch for ch in ch_list if not authz.can_read(auth_result, ch)]
                if forbidden:
                    return _error(
                        "Forbidden",
                        f"Token for '{auth_result}' cannot subscribe to: {', '.join(forbidden)}",
                        403,
                    )

            # Parse Last-Event-ID for cursor-based resume
            last_event_id = request.headers.get("last-event-id", "").strip() or None

            # Opt-in own-message filter, for consumers that do not use the
            # SDK.  Compared against the authenticated identity, never a
            # client-supplied user id; no effect for supertoken/no-auth.
            exclude_own_param = (request.query_params.get("exclude_own") or ["false"])[0]
            exclude_own = exclude_own_param.strip().lower() == "true"

            q, sub_ids, drop_lock, drop_counter = _setup_sse_subscriptions(
                bus, ch_list, auth_result, exclude_own
            )

            on_activity = (
                partial(self._note_activity, auth_result) if isinstance(auth_result, str) else None
            )

            return StreamingResponse(
                _sse_event_generator(
                    _SSESession(
                        queue=q,
                        sub_ids=sub_ids,
                        bus=bus,
                        channels=ch_list,
                        auth_result=auth_result,
                        last_event_id=last_event_id,
                        drop_lock=drop_lock,
                        drop_counter=drop_counter,
                        exclude_own=exclude_own,
                        on_activity=on_activity,
                    )
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

            ``exclude_own=true`` (default false) drops messages sent by the
            authenticated caller.  The per-channel endpoint accepts the same
            flag.
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
                return _error("Bad Request", "Channel name required", 400)
            return await _handle_subscribe(request, [channel])


async def _require_acl_manage(
    auth_result: Any, authz: ChannelAuthorizer, channel: str
) -> tuple[dict, int] | None:
    """Check that the current user may manage *channel*.

    Returns an error tuple (dict, status) if denied, or None if allowed.
    Supertokens and no-auth mode always pass.
    """
    if isinstance(auth_result, str) and not await asyncio.to_thread(
        authz.can_manage_acl, auth_result, channel
    ):
        return _error(
            "Forbidden", f"Agent '{auth_result}' lacks manage permission on '{channel}'", 403
        )
    return None


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


def _filter_visible_messages(auth_result: Any, msgs: list[Message]) -> list[Message]:
    """Filter messages to only those visible to the authenticated agent.

    Supertokens and no-auth mode see all messages.  Scoped tokens
    only see messages where ``_user_involved`` returns True.
    """
    if not isinstance(auth_result, str):
        return msgs
    return [m for m in msgs if _user_involved(auth_result, m.channel, m.sender)]


def _setup_sse_subscriptions(
    bus: Bus, ch_list: list[str], auth_result: Any, exclude_own: bool = False
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
                if isinstance(auth_result, str) and not _user_involved(
                    auth_result, msg.channel, msg.sender
                ):
                    return
                if exclude_own and msg.sender == auth_result:
                    return
                data = stream_event_to_json(StreamEvent(channel=target_ch, message=msg))
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
    """Return an SSE warning comment if events were dropped, else empty string.

    The warning is emitted after the next live event or on the next
    keepalive tick, whichever comes first, so a stream that goes quiet
    after drops still reports them within the keepalive interval.
    """
    if drop_lock is None or drop_counter is None:
        return ""
    with drop_lock:
        dropped = drop_counter[0]
        drop_counter[0] = 0
    if dropped:
        return f": warning: {dropped} event(s) dropped (slow consumer), re-query for gaps\n\n"
    return ""


async def _replay_missed_events(session: _SSESession) -> Any:
    """Yield SSE events for messages published after ``last_event_id``.

    When a channel's replay fills ``_SSE_REPLAY_LIMIT``, a warning comment
    precedes that channel's events.  The query cannot tell whether more
    messages exist beyond the limit, so the client should re-query; the
    warning can be a false positive when exactly the limit is waiting.
    """
    for ch in session.channels:
        missed = await asyncio.to_thread(
            session.bus.query, ch, after=session.last_event_id, limit=_SSE_REPLAY_LIMIT
        )
        if len(missed) >= _SSE_REPLAY_LIMIT:
            yield (
                f": warning: replay truncated at {_SSE_REPLAY_LIMIT} "
                f"for channel '{ch}', re-query for more\n\n"
            )
        if isinstance(session.auth_result, str):
            missed = [m for m in missed if _user_involved(session.auth_result, m.channel, m.sender)]
            if session.exclude_own:
                missed = [m for m in missed if m.sender != session.auth_result]
        for m in missed:
            yield _format_sse_event(stream_event_to_json(StreamEvent(channel=ch, message=m)), m.id)


async def _sse_event_generator(session: _SSESession) -> Any:
    """Async generator that yields SSE events from the queue.

    If *last_event_id* is set (via ``Last-Event-ID`` header), replays
    missed messages from the bus before switching to live streaming.
    Each event includes an ``id:`` field (the message ID) so the
    W3C SSE client can resume on reconnect.

    When events are dropped due to slow consumption, a warning SSE
    comment is emitted after the next event or keepalive tick so the
    client knows to re-query for any gaps.  A replay that fills
    ``_SSE_REPLAY_LIMIT`` is also flagged as possibly truncated.
    """
    try:
        yield ": connected\n\n"

        # ── Replay missed messages on reconnect ──────────────────
        if session.last_event_id:
            async for event in _replay_missed_events(session):
                yield event

        # NOTE: Subscriptions are active during replay, so a message
        # published in the replay window can appear in both the replay
        # results and the live queue (duplicate).  SSE clients that track
        # ``lastEventId`` deduplicate naturally; for others the window
        # is small and payloads are idempotent.

        # ── Live stream ──────────────────────────────────────────
        while True:
            # An open subscription is evidence the subscriber is present,
            # even while it sends no requests of its own.
            if session.on_activity is not None:
                session.on_activity()
            try:
                data = await asyncio.wait_for(session.queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                # Report drops even when the stream is otherwise quiet.
                warning = _drain_drop_counter(session.drop_lock, session.drop_counter)
                if warning:
                    yield warning
                continue
            if data is None:
                break
            # The SSE id: line carries the message id.
            try:
                event_id = parse_stream_event(data).message.id
            except ContractError:
                event_id = ""
            yield _format_sse_event(data, event_id)

            # Notify client about dropped events (slow consumer)
            warning = _drain_drop_counter(session.drop_lock, session.drop_counter)
            if warning:
                yield warning
    finally:
        for sid in session.sub_ids:
            session.bus.unsubscribe(sid)
