"""HTTP adapter for mansio — REST API + SSE push.

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

Route registration:
    ``HttpAdapter`` is the composition root.  ``attach()`` installs the
    middleware, builds one frozen
    :class:`~mansio_web.adapters.routes.RouteContext`, then calls the
    route modules in a fixed order.  That order — middleware → RouteContext
    → registers — defines the public route table, so adding a route means
    editing the matching module, not this class.

Authentication:
    All /v1/* endpoints (except /health and /v1/auth/check) require
    ``Authorization: Bearer mst-...``. The token is validated server-side,
    and the sender field in publish requests must match the token's user_id.
    Supertokens (user_id=NULL) bypass sender checks.

    An adapter with no TokenStore cannot validate anything, so it refuses
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
import time
from typing import TYPE_CHECKING

from mansio_core.authz import ChannelAuthorizer
from mansio_core.principal import Principal

from mansio_web._vendor.httpserver import App, JSONResponse, Response, StreamingResponse
from mansio_web._vendor.structlog import get_logger
from mansio_web.adapters.routes import RouteContext
from mansio_web.adapters.routes import auth as auth_routes
from mansio_web.adapters.routes import channel_admin as channel_admin_routes
from mansio_web.adapters.routes import channels as channel_routes
from mansio_web.adapters.routes import health as health_routes
from mansio_web.adapters.routes import messages as message_routes
from mansio_web.adapters.routes import presence as presence_routes
from mansio_web.adapters.routes import publish as publish_routes
from mansio_web.adapters.routes import queue as queue_routes
from mansio_web.adapters.routes import registry as registry_routes
from mansio_web.adapters.routes import sse as sse_routes
from mansio_web.adapters.routes._common import _error_body

if TYPE_CHECKING:
    from mansio_core.bus import Bus
    from mansio_core.token_store import TokenStore

    from mansio_web._vendor.httpserver import Request

logger = get_logger(__name__)

# Paths that skip token auth
_PUBLIC_PATHS = frozenset({"/health", "/v1/auth/check"})

# Returned for every authenticated endpoint when the adapter has no way to
# authenticate anyone. Both causes are the same failure from a caller's side —
# no token can be presented that would work — so they answer alike.
_NO_TOKEN_STORE_MESSAGE = (
    "This server has no token store, so it cannot authenticate anyone and "
    "refuses all authenticated endpoints. Configure a TokenStore, or pass "
    "allow_unauthenticated=True to HttpAdapter to serve an open API on purpose."
)
_NO_TOKENS_MESSAGE = (
    "No API tokens configured. Create a token via the admin panel before connecting."
)


def _principal_from_validation(result: str | None) -> Principal:
    """Map ``TokenStore.validate()`` to the caller principal.

    The mapping is closed on purpose: an unexpected value raises instead of
    falling back to a privileged principal.
    """
    if isinstance(result, str):
        return Principal.user(result)
    if result is None:
        return Principal.supertoken()
    raise TypeError(f"unexpected token validation result: {result!r}")


# Shortest interval between two presence writes for the same user. last_seen
# only has to stay fresh enough that users() still counts the user as online,
# so this sits well inside that presence timeout while holding even a busy
# client to one write per interval.
_PRESENCE_REFRESH_SECONDS = 30.0


class HttpAdapter:
    """HTTP adapter — REST API + SSE push notifications.

    Exposes a Bus over HTTP so remote MansioClient instances
    can connect via HttpTransport.

    Args:
        host: Bind address. Default "127.0.0.1".
        port: Bind port. Default 8742.
        cors_origin: Access-Control-Allow-Origin value. Default "*".
        max_body_bytes: Maximum request body size in bytes. Default 1MB.
        max_query_limit: Maximum query limit parameter. Default 10000.
        token_store: TokenStore used to authenticate API requests. Without
            one the adapter rejects every authenticated endpoint.
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
        """Bind this adapter to a Bus and register routes.

        The order is fixed: middleware, then one frozen RouteContext, then
        the route modules.  That order defines the public route table.

        Args:
            bus: The Bus to expose over HTTP.

        Raises:
            RuntimeError: If already attached.
        """
        if self._bus is not None:
            raise RuntimeError("Adapter already attached to a bus")
        self._bus = bus
        self._authz = ChannelAuthorizer(bus)
        self._setup_middleware()
        ctx = RouteContext(
            bus=bus,
            authz=self._authz,
            token_store=self._token_store,
            allow_unauthenticated=self._allow_unauthenticated,
            max_query_limit=self._max_query_limit,
            cors_origin=self._cors_origin,
            note_activity=self._note_activity,
        )
        for module in (
            health_routes,
            publish_routes,
            channel_routes,
            auth_routes,
            message_routes,
            registry_routes,
            queue_routes,
            presence_routes,
            channel_admin_routes,
            sse_routes,
        ):
            module.register(self._app, ctx)

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
        """Return the (host, port) this adapter is bound to."""
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
        return f"HttpAdapter({addr}, {status}, {auth})"

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
                    request.state.principal = Principal.no_auth()
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

            # validate() returns a scoped user id or None (supertoken).
            request.state.principal = _principal_from_validation(result)
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
