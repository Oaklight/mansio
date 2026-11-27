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

from mansio_web._vendor.httpserver import App, JSONResponse, Response
from mansio_web._vendor.structlog import get_logger

from .auth import SessionAuth
from .routes import RouteContext
from .routes import auth as auth_routes
from .routes import channels as channel_routes
from .routes import messages as message_routes
from .routes import prefixes as prefix_routes
from .routes import stats as stats_routes
from .routes import subscriptions as subscription_routes
from .routes import system as system_routes
from .routes import tokens as token_routes
from .routes import ui as ui_routes
from .routes import users as user_routes
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
    """Admin panel HTTP server implementing the Adapter protocol.

    Can be used standalone (legacy ``bus`` in constructor) or as a
    Adapter via ``attach(bus)`` + ``serve_forever()``.

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

    # ── Adapter protocol ─────────────────────────────────────────

    def attach(self, bus: Bus) -> None:
        """Bind this adapter to a Bus instance.

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
        """Return the (host, port) this adapter is listening on.

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
        """Register all admin routes through the route modules."""
        bus = self._bus
        view = self._view
        assert bus is not None
        assert view is not None
        ctx = RouteContext(
            view=view,
            bus=bus,
            token_store=self._token_store,
            auth=self._auth,
            serve_ui=self._serve_ui,
        )
        for module in (
            ui_routes,
            auth_routes,
            stats_routes,
            channel_routes,
            message_routes,
            subscription_routes,
            system_routes,
            prefix_routes,
            user_routes,
            token_routes,
        ):
            module.register(self._app, ctx)
