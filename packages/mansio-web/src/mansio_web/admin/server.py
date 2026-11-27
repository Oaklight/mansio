"""Admin panel HTTP server.

Uses vendored zerodep ``httpserver`` — async, decorator-based routing.
Provides dashboard stats, channel browsing, message inspection,
token management, and optional web UI.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mansio_core.contracts import ContractError, parse_publish
from mansio_core.contracts.admin import (
    parse_admin_messages,
    parse_prefix_create,
    parse_token_create,
    parse_user_create,
    parse_user_token_create,
)

from mansio_web._vendor.httpserver import App, JSONResponse, Response
from mansio_web._vendor.structlog import get_logger

from .auth import SessionAuth
from .view import AdminView

if TYPE_CHECKING:
    from mansio_core.bus import Bus
    from mansio_core.token_store import TokenStore

logger = get_logger(__name__)


def _redact_password(value: str, tail: int = 4) -> str:
    """Mask a password for safe logging, showing only the last *tail* chars."""
    if len(value) <= tail:
        return "****"
    return "*" * (len(value) - tail) + value[-tail:]


@dataclass
class AdminInfo:
    """Information about the running admin server."""

    host: str
    port: int
    url: str
    password: str | None

    def __repr__(self) -> str:
        pw = _redact_password(self.password) if self.password else "None"
        return f"AdminInfo(host={self.host!r}, port={self.port!r}, url={self.url!r}, password={pw})"


class AdminServer:
    """Admin panel HTTP server implementing the Frontend protocol.

    Can be used standalone (legacy ``bus`` in constructor) or as a
    Frontend via ``attach(bus)`` + ``serve_forever()``.

    Args:
        bus: Optional Bus instance. If provided, ``attach(bus)`` is
            called automatically for backward compatibility.
        host: Host address to bind to. Defaults to ``"127.0.0.1"``.
        port: Port number to listen on. Defaults to 8741.
        serve_ui: Whether to serve the admin UI at root path.
        remote: Whether to allow remote connections (binds to 0.0.0.0).
        auth_password: Optional admin password. Auto-generated if remote=True.
        token_store: Optional TokenStore for agent token management.
    """

    def __init__(
        self,
        bus: Bus | None = None,
        host: str = "127.0.0.1",
        port: int = 8741,
        serve_ui: bool = True,
        remote: bool = False,
        auth_password: str | None = None,
        token_store: TokenStore | None = None,
        auth_token: str | None = None,
    ) -> None:
        self._bus: Bus | None = None
        self._view: AdminView | None = None
        self._host = "0.0.0.0" if remote else host
        self._port = port
        self._serve_ui = serve_ui
        self._token_store = token_store

        password = auth_password or auth_token
        if password is not None:
            self._auth: SessionAuth | None = SessionAuth(password)
        elif remote:
            self._auth = SessionAuth()
        else:
            self._auth = None

        self._app = App()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

        self._setup_middleware()

        # Backward compat: if bus is passed to constructor, attach immediately
        if bus is not None:
            self.attach(bus)

    # ── Frontend protocol ─────────────────────────────────────────

    def attach(self, bus: Bus) -> None:
        """Bind this frontend to a Bus instance.

        Must be called before ``serve_forever()`` or ``start()``.
        Sets up all admin API routes that depend on the bus.

        Args:
            bus: The Bus to expose.

        Raises:
            RuntimeError: If already attached.
        """
        if self._bus is not None:
            raise RuntimeError("AdminServer already attached to a bus")
        self._bus = bus
        self._view = AdminView(bus, self._token_store)
        self._setup_routes()

    def serve_forever(self) -> None:
        """Start accepting connections. Blocks until ``shutdown()``.

        Raises:
            RuntimeError: If not attached to a Bus.
        """
        if self._bus is None:
            raise RuntimeError("Must call attach(bus) before serve_forever()")
        self._started.set()
        self._app.run(self._host, self._port)

    def shutdown(self) -> None:
        """Stop accepting connections and release resources.

        Safe to call multiple times or if not running.
        """
        self._app.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
            self._started.clear()
            logger.info("Admin server stopped")

    @property
    def address(self) -> tuple[str, int]:
        """Return the (host, port) this frontend is listening on.

        Raises:
            RuntimeError: If not yet serving.
        """
        h = self._app.host or self._host
        p = self._app.port if self._app.port is not None else self._port
        return (h, p)

    # ── Convenience lifecycle (background thread) ─────────────────

    def start(self) -> AdminInfo:
        """Start the server in a background thread.

        Convenience method that launches ``serve_forever()`` in a
        daemon thread. The bus must already be attached (either via
        constructor or ``attach()``).
        """
        if self._bus is None:
            raise RuntimeError("Must call attach(bus) before start()")
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Server is already running")

        if self._port != 0:
            self._port = self.find_available_port(self._host, self._port)

        self._thread = threading.Thread(target=self._run, daemon=True, name="mansio-admin")
        self._thread.start()
        self._started.wait(timeout=5.0)

        # Wait for App to bind and expose actual port (especially for port=0)
        for _ in range(50):
            if self._app.port is not None and self._app.port != 0:
                self._port = self._app.port
                break
            time.sleep(0.01)
        else:
            if self._port == 0:
                logger.warning("Server did not report bound port within 500ms")

        display_host = "localhost" if self._host in ("0.0.0.0", "127.0.0.1") else self._host
        url = f"http://{display_host}:{self._port}"

        info = AdminInfo(
            host=self._host,
            port=self._port,
            url=url,
            password=self._auth.password if self._auth else None,
        )

        if self._auth and self._auth.password:
            logger.info(
                "Admin server started",
                url=url,
                password=_redact_password(self._auth.password),
            )
        else:
            logger.info("Admin server started", url=url)

        return info

    def _run(self) -> None:
        """Run the async server (called in background thread)."""
        self._started.set()
        self._app.run(self._host, self._port)

    def stop(self) -> None:
        """Stop the server. Safe to call if not running.

        Alias for ``shutdown()`` — kept for backward compatibility.
        """
        self.shutdown()

    def is_running(self) -> bool:
        """Check if server is running."""
        return self._thread is not None and self._started.is_set()

    def get_info(self) -> AdminInfo | None:
        """Get server info if running."""
        if not self.is_running():
            return None
        display_host = "localhost" if self._host in ("0.0.0.0", "127.0.0.1") else self._host
        return AdminInfo(
            host=self._host,
            port=self._port,
            url=f"http://{display_host}:{self._port}",
            password=self._auth.password if self._auth else None,
        )

    @staticmethod
    def find_available_port(host: str, start_port: int) -> int:
        """Find an available port starting from start_port."""
        for port in range(start_port, start_port + 100):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((host, port))
                    return port
            except OSError:
                continue
        raise RuntimeError(f"Could not find available port in range {start_port}-{start_port + 99}")

    # ── Middleware ────────────────────────────────────────────────

    def _setup_middleware(self) -> None:
        """Register CORS + session-cookie auth middleware."""
        auth = self._auth
        cors_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        }

        @self._app.before_request
        def cors_and_auth(request: Any) -> Response | None:
            # CORS preflight
            if request.method == "OPTIONS":
                return Response(status_code=204, headers=cors_headers.copy())

            # No auth configured → pass through
            if auth is None:
                return None

            path = request.path.split("?")[0]
            if path in ("/api/login", "/api/logout", "/api/auth-check"):
                return None
            if not path.startswith("/api/"):
                return None

            session_token = request.cookies.get(SessionAuth.COOKIE_NAME)
            if session_token and auth.validate_session(session_token):
                return None

            return JSONResponse(
                {"error": "Unauthorized", "message": "Admin authentication required"},
                status_code=401,
                headers=cors_headers.copy(),
            )

        @self._app.after_request
        def add_cors(request: Any, response: Any) -> Any:
            if hasattr(response, "headers"):
                for k, v in cors_headers.items():
                    response.headers.setdefault(k, v)
            return response

    # ── Routes ───────────────────────────────────────────────────

    def _setup_routes(self) -> None:
        """Register all admin routes."""
        self._setup_ui_routes()
        self._setup_auth_routes()
        self._setup_dashboard_routes()
        self._setup_channel_routes()
        self._setup_message_routes()
        self._setup_subscription_routes()
        self._setup_system_routes()
        self._setup_prefix_routes()
        self._setup_user_routes()
        self._setup_user_token_routes()
        self._setup_token_routes()

    def _setup_ui_routes(self) -> None:
        """Root path — serve admin HTML or API index; serve UI assets."""
        serve_ui = self._serve_ui

        @self._app.get("/assets/<path:path>")
        def asset(request: Any, path: str) -> Response | tuple:
            from .static import get_asset

            loaded = get_asset(path) if serve_ui else None
            if loaded is None:
                return {
                    "error": "Not Found",
                    "message": f"Asset '{path}' not found",
                }, 404
            body, content_type = loaded
            return Response(
                body,
                content_type=content_type,
                headers={"Cache-Control": "no-cache"},
            )

        @self._app.get("/")
        def root(request: Any) -> Response | dict:
            if serve_ui:
                from .static import ADMIN_HTML

                return Response(ADMIN_HTML, content_type="text/html")
            return {
                "name": "Mansio Admin API",
                "version": "1.0.0",
                "endpoints": [
                    "GET /api/stats",
                    "GET /api/stats/throughput",
                    "GET /api/channels",
                    "GET /api/channels/{name}",
                    "GET /api/messages?channel=...",
                    "POST /api/messages",
                    "GET /api/subscriptions",
                    "GET /api/users",
                    "POST /api/users",
                    "GET /api/users/{user_id}",
                    "DELETE /api/users/{user_id}",
                    "GET /api/users/{user_id}/tokens",
                    "POST /api/users/{user_id}/tokens",
                    "GET /api/tokens",
                    "POST /api/tokens",
                ],
            }

    def _setup_auth_routes(self) -> None:
        """Login, logout, auth-check routes."""
        auth = self._auth

        @self._app.get("/api/auth-check")
        def auth_check(request: Any) -> dict:
            if auth is None:
                return {"authenticated": True, "required": False}
            session_token = request.cookies.get(SessionAuth.COOKIE_NAME)
            authenticated = bool(session_token and auth.validate_session(session_token))
            return {"authenticated": authenticated, "required": True}

        @self._app.post("/api/login")
        def login(request: Any) -> Response | dict | tuple:
            if auth is None:
                return {"ok": True}
            data = request.json()
            password = data.get("password", "")

            client_ip = request.client_addr[0]
            wait = auth._check_rate_limit(client_ip)
            if wait > 0:
                return {"error": "Too many attempts", "retry_after": round(wait, 1)}, 429

            if not auth.check_password(password):
                auth._record_failure(client_ip)
                return {"error": "Invalid password"}, 401

            auth._clear_failures(client_ip)
            session_token = auth.create_session()
            resp = JSONResponse({"ok": True})
            resp.set_cookie(
                SessionAuth.COOKIE_NAME,
                session_token,
                httponly=True,
                samesite="Strict",
                path="/",
            )
            return resp

        @self._app.post("/api/logout")
        def logout(request: Any) -> Response:
            if auth:
                session_token = request.cookies.get(SessionAuth.COOKIE_NAME)
                if session_token:
                    auth.revoke_session(session_token)
            resp = JSONResponse({"ok": True})
            resp.delete_cookie(SessionAuth.COOKIE_NAME, path="/")
            return resp

    def _setup_dashboard_routes(self) -> None:
        """Stats and throughput routes."""
        view = self._view
        assert view is not None

        @self._app.get("/api/stats")
        def stats(request: Any) -> dict:
            return view.stats().to_dict()

        @self._app.get("/api/stats/throughput")
        def throughput(request: Any) -> dict:
            return view.throughput().to_dict()

    def _setup_channel_routes(self) -> None:
        """Channel listing and detail routes."""
        view = self._view
        assert view is not None

        @self._app.get("/api/channels")
        def channels(request: Any) -> dict:
            return view.channels().to_dict()

        @self._app.get("/api/channels/<name>")
        def channel_detail(request: Any, name: str) -> dict | tuple:
            detail = view.channel_detail(name)
            if detail is None:
                return {"error": "Not Found", "message": f"Channel '{name}' not found"}, 404
            return detail.to_dict()

    def _setup_message_routes(self) -> None:
        """Message query and publish routes."""
        bus = self._bus

        @self._app.get("/api/messages")
        def get_messages(request: Any) -> dict | tuple:
            try:
                query = parse_admin_messages(request.query_params)
            except ContractError as err:
                return err.as_error(), 400
            view = self._view
            assert view is not None
            return view.messages(query).to_dict()

        @self._app.post("/api/messages")
        def publish_message(request: Any) -> dict | tuple:
            try:
                payload = parse_publish(request.json())
            except ContractError as err:
                return err.as_error(), 400
            msg_id = bus.publish(
                channel=payload.channel,
                sender=payload.sender,
                msg_type=payload.msg_type,
                payload=payload.payload,
                metadata=payload.metadata,
            )
            return {"success": True, "message_id": msg_id}

    def _setup_subscription_routes(self) -> None:
        """Subscription listing route."""

        view = self._view
        assert view is not None

        @self._app.get("/api/subscriptions")
        def subscriptions(request: Any) -> dict:
            return view.subscriptions().to_dict()

    def _setup_system_routes(self) -> None:
        """System/backend info route."""
        view = self._view
        assert view is not None

        @self._app.get("/api/system")
        def system_info(request: Any) -> dict:
            return view.system().to_dict()

    def _setup_prefix_routes(self) -> None:
        """Channel prefix registration routes (admin-only)."""
        token_store = self._token_store

        view = self._view
        assert view is not None

        @self._app.get("/api/prefixes")
        def list_prefixes(request: Any) -> dict:
            return view.prefixes().to_dict()

        @self._app.post("/api/prefixes")
        def register_prefix(request: Any) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            try:
                payload = parse_prefix_create(request.json() if request.body else {})
            except ContractError as err:
                return err.as_error(), 400
            result = token_store.register_prefix(prefix=payload.prefix, label=payload.label)
            if result is None:
                return {
                    "error": "Bad Request",
                    "message": f"'{payload.prefix}' is a built-in prefix and cannot be registered",
                }, 400
            return view.prefix_created(result).to_dict(), 201

        @self._app.delete("/api/prefixes/<prefix>")
        def delete_prefix(request: Any, prefix: str) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            if token_store.delete_prefix(prefix):
                return view.prefix_deleted(prefix).to_dict()
            return {
                "error": "Not Found",
                "message": f"Prefix '{prefix}' not found or is built-in",
            }, 404

    def _setup_user_routes(self) -> None:
        """Register user CRUD routes."""
        self._setup_user_list_register_routes()
        self._setup_user_detail_delete_routes()

    def _setup_user_list_register_routes(self) -> None:
        """User list and registration routes."""
        token_store = self._token_store

        view = self._view
        assert view is not None

        @self._app.get("/api/users")
        def list_users(request: Any) -> dict:
            return view.users().to_dict()

        @self._app.post("/api/users")
        def register_user(request: Any) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            try:
                payload = parse_user_create(request.json() if request.body else {})
            except ContractError as err:
                return err.as_error(), 400
            if token_store.user_exists(payload.user_id):
                return {
                    "error": "Conflict",
                    "message": f"User '{payload.user_id}' already exists",
                }, 409
            entry = token_store.create_token(user_id=payload.user_id, label=payload.label)
            return view.user_registered(payload.user_id, entry).to_dict(), 201

    def _setup_user_detail_delete_routes(self) -> None:
        """User detail and deletion routes."""
        token_store = self._token_store

        view = self._view
        assert view is not None

        @self._app.get("/api/users/<user_id>")
        def get_user(request: Any, user_id: str) -> dict | tuple:
            if token_store is None:
                return {"error": "Service Unavailable"}, 503
            detail = view.user_detail(user_id)
            if detail is None:
                return {"error": "Not Found", "message": f"User '{user_id}' not found"}, 404
            return detail.to_dict()

        @self._app.delete("/api/users/<user_id>")
        def delete_user(request: Any, user_id: str) -> dict | tuple:
            if token_store is None:
                return {"error": "Service Unavailable"}, 503
            count = token_store.delete_user(user_id)
            if count == 0:
                return {"error": "Not Found", "message": f"User '{user_id}' not found"}, 404
            return view.user_deleted(count).to_dict()

    def _setup_user_token_routes(self) -> None:
        """User token sub-resource routes."""
        token_store = self._token_store

        view = self._view
        assert view is not None

        @self._app.get("/api/users/<user_id>/tokens")
        def list_user_tokens(request: Any, user_id: str) -> dict | tuple:
            if token_store is None:
                return {"error": "Service Unavailable"}, 503
            return view.user_tokens(user_id).to_dict()

        @self._app.post("/api/users/<user_id>/tokens")
        def create_user_token(request: Any, user_id: str) -> dict | tuple:
            if token_store is None:
                return {"error": "Service Unavailable"}, 503
            if not token_store.user_exists(user_id):
                return {"error": "Not Found", "message": f"User '{user_id}' not found"}, 404
            try:
                label = parse_user_token_create(request.json() if request.body else {})
            except ContractError as err:
                return err.as_error(), 400
            entry = token_store.create_token(user_id=user_id, label=label)
            return view.user_token_created(entry).to_dict(), 201

    def _setup_token_routes(self) -> None:
        """Token CRUD routes."""
        token_store = self._token_store

        view = self._view
        assert view is not None

        @self._app.get("/api/tokens")
        def list_tokens(request: Any) -> dict:
            return view.tokens().to_dict()

        @self._app.post("/api/tokens")
        def create_token(request: Any) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            try:
                payload = parse_token_create(request.json() if request.body else {})
            except ContractError as err:
                return err.as_error(), 400
            if payload.supertoken:
                entry = token_store.create_token(user_id=None, label=payload.label)
                return view.token_created(entry, supertoken=True).to_dict(), 201
            entry = token_store.create_token(user_id=payload.user_id, label=payload.label)
            return view.token_created(entry).to_dict(), 201

        @self._app.delete("/api/tokens/<token_id>")
        def delete_token(request: Any, token_id: str) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            if token_store.delete_token(token_id):
                return view.token_deleted(token_id).to_dict()
            return {"error": "Not Found", "message": f"Token '{token_id}' not found"}, 404

        @self._app.post("/api/tokens/<token_id>/rotate")
        def rotate_token(request: Any, token_id: str) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            result = token_store.rotate_token(token_id)
            if result is None:
                return {"error": "Not Found", "message": f"Token '{token_id}' not found"}, 404
            return view.token_rotated(result).to_dict()
