"""Admin panel HTTP server.

Uses vendored zerodep ``httpserver`` — async, decorator-based routing.
Provides dashboard stats, channel browsing, message inspection,
token management, and optional web UI.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mansio._vendor.httpserver import App, JSONResponse, Response
from mansio._vendor.structlog import get_logger

from .auth import SessionAuth

if TYPE_CHECKING:
    from mansio.bus import Bus
    from mansio.token_store import TokenStore

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
    """Admin panel HTTP server.

    Args:
        bus: The Bus instance to monitor.
        host: Host address to bind to. Defaults to "127.0.0.1".
        port: Port number to listen on. Defaults to 8741.
        serve_ui: Whether to serve the admin UI at root path.
        remote: Whether to allow remote connections (binds to 0.0.0.0).
        auth_password: Optional admin password. Auto-generated if remote=True.
        token_store: Optional TokenStore for agent token management.
    """

    def __init__(
        self,
        bus: Bus,
        host: str = "127.0.0.1",
        port: int = 8741,
        serve_ui: bool = True,
        remote: bool = False,
        auth_password: str | None = None,
        token_store: TokenStore | None = None,
        auth_token: str | None = None,
    ) -> None:
        self._bus = bus
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
        self._setup_routes()

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> AdminInfo:
        """Start the server in a background thread."""
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
        """Stop the server. Safe to call if not running."""
        self._app.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
            self._started.clear()
            logger.info("Admin server stopped")

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

            cookie_header = request.headers.get("cookie", "")
            session_token = _extract_session_cookie(cookie_header)
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
        """Root path — serve admin HTML or API index."""
        serve_ui = self._serve_ui

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
                ],
            }

    def _setup_auth_routes(self) -> None:
        """Login, logout, auth-check routes."""
        auth = self._auth

        @self._app.get("/api/auth-check")
        def auth_check(request: Any) -> dict:
            if auth is None:
                return {"authenticated": True, "required": False}
            cookie_header = request.headers.get("cookie", "")
            session_token = _extract_session_cookie(cookie_header)
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
            return Response(
                body=json.dumps({"ok": True}),
                content_type="application/json; charset=utf-8",
                headers={
                    "Set-Cookie": f"{SessionAuth.COOKIE_NAME}={session_token}; HttpOnly; SameSite=Strict; Path=/",
                },
            )

        @self._app.post("/api/logout")
        def logout(request: Any) -> Response:
            if auth:
                cookie_header = request.headers.get("cookie", "")
                session_token = _extract_session_cookie(cookie_header)
                if session_token:
                    auth.revoke_session(session_token)
            return Response(
                body=json.dumps({"ok": True}),
                content_type="application/json; charset=utf-8",
                headers={
                    "Set-Cookie": f"{SessionAuth.COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
                },
            )

    def _setup_dashboard_routes(self) -> None:
        """Stats and throughput routes."""
        bus = self._bus

        @self._app.get("/api/stats")
        def stats(request: Any) -> dict:
            s = bus.backend.stats()
            s["active_subscriptions"] = sum(len(v) for v in bus.subscription_counts().values())
            return s

        @self._app.get("/api/stats/throughput")
        def throughput(request: Any) -> dict:
            window = 60
            buckets = bus.metrics.get_throughput(window)
            return {"window_seconds": window, "buckets": buckets}

    def _setup_channel_routes(self) -> None:
        """Channel listing and detail routes."""
        bus = self._bus

        @self._app.get("/api/channels")
        def channels(request: Any) -> dict:
            result = []
            sub_counts = self._bus.subscription_counts()
            for ch_name in bus.channels():
                msgs = bus.query(ch_name, limit=1000)
                senders = {m.sender for m in msgs}
                last_time = msgs[-1].timestamp if msgs else None
                result.append(
                    {
                        "name": ch_name,
                        "message_count": len(msgs),
                        "sender_count": len(senders),
                        "subscription_count": len(sub_counts.get(ch_name, [])),
                        "last_message_time": last_time,
                    }
                )
            return {"channels": result}

        @self._app.get("/api/channels/<name>")
        def channel_detail(request: Any, name: str) -> dict | tuple:
            msgs = bus.query(name, limit=1000)
            if not msgs:
                return {"error": "Not Found", "message": f"Channel '{name}' not found"}, 404
            senders = sorted({m.sender for m in msgs})
            type_counts: dict[str, int] = {}
            for m in msgs:
                type_counts[m.msg_type] = type_counts.get(m.msg_type, 0) + 1
            return {
                "name": name,
                "message_count": len(msgs),
                "senders": senders,
                "msg_types": [{"msg_type": t, "count": c} for t, c in type_counts.items()],
                "last_message_time": msgs[-1].timestamp if msgs else None,
            }

    def _setup_message_routes(self) -> None:
        """Message query and publish routes."""
        bus = self._bus

        @self._app.get("/api/messages")
        def get_messages(request: Any) -> dict | tuple:
            channel = (request.query_params.get("channel") or [None])[0]
            if not channel:
                return {"error": "Bad Request", "message": "Query param 'channel' required"}, 400
            sender = (request.query_params.get("sender") or [None])[0]
            msg_type = (request.query_params.get("msg_type") or [None])[0]
            try:
                limit = int((request.query_params.get("limit") or ["100"])[0])
            except ValueError:
                return {"error": "Bad Request", "message": "'limit' must be an integer"}, 400

            msgs = bus.backend.search(
                channel=channel, sender=sender, msg_type=msg_type, limit=limit
            )
            return {
                "count": len(msgs),
                "messages": [_msg_dict(m) for m in msgs],
            }

        @self._app.post("/api/messages")
        def publish_message(request: Any) -> dict | tuple:
            data = request.json()
            required = ("channel", "sender", "msg_type", "payload")
            missing = [f for f in required if f not in data]
            if missing:
                return {"error": "Bad Request", "message": f"Missing: {', '.join(missing)}"}, 400
            msg_id = bus.publish(
                channel=data["channel"],
                sender=data["sender"],
                msg_type=data["msg_type"],
                payload=data["payload"],
                metadata=data.get("metadata"),
            )
            return {"success": True, "message_id": msg_id}

    def _setup_subscription_routes(self) -> None:
        """Subscription listing route."""

        @self._app.get("/api/subscriptions")
        def subscriptions(request: Any) -> dict:
            counts = self._bus.subscription_counts()
            ch_list = [
                {"channel": ch, "subscription_ids": ids, "count": len(ids)}
                for ch, ids in counts.items()
            ]
            total = sum(len(ids) for ids in counts.values())
            return {"total": total, "channels": ch_list}

    def _setup_system_routes(self) -> None:
        """System/backend info route."""
        bus = self._bus
        token_store = self._token_store

        @self._app.get("/api/system")
        def system_info(request: Any) -> dict:
            import mansio

            info: dict = {
                "version": mansio.__version__,
                "backend": {},
                "tokens": {"configured": 0},
            }
            info["backend"] = bus.backend.info()
            if token_store:
                info["tokens"]["configured"] = len(token_store.list_tokens())
            return info

    def _setup_prefix_routes(self) -> None:
        """Channel prefix registration routes (admin-only)."""
        token_store = self._token_store

        @self._app.get("/api/prefixes")
        def list_prefixes(request: Any) -> dict:
            if token_store is None:
                return {"prefixes": [], "enabled": False}
            return {"prefixes": token_store.list_prefixes(), "enabled": True}

        @self._app.post("/api/prefixes")
        def register_prefix(request: Any) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            data = request.json() if request.body else {}
            prefix = (data.get("prefix") or "").strip().lower()
            if (
                not prefix
                or len(prefix) < 2
                or len(prefix) > 32
                or not re.match(r"^[a-z][a-z0-9]{1,31}$", prefix)
            ):
                return {
                    "error": "Bad Request",
                    "message": "Prefix must be 2-32 lowercase alphanumeric, start with letter",
                }, 400
            result = token_store.register_prefix(prefix=prefix, label=data.get("label", ""))
            if result is None:
                return {
                    "error": "Bad Request",
                    "message": f"'{prefix}' is a built-in prefix and cannot be registered",
                }, 400
            return {"ok": True, "prefix": result}, 201

        @self._app.delete("/api/prefixes/<prefix>")
        def delete_prefix(request: Any, prefix: str) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            if token_store.delete_prefix(prefix):
                return {"ok": True, "deleted": prefix}
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
        bus = self._bus

        _USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

        def _validate_user_id(user_id: str) -> tuple | None:
            if not _USER_ID_RE.match(user_id):
                return {
                    "error": "Bad Request",
                    "message": "user_id must be 3-64 chars, lowercase alphanumeric + hyphens",
                }, 400
            return None

        @self._app.get("/api/users")
        def list_users(request: Any) -> dict:
            if token_store is None:
                return {"users": [], "enabled": False}
            users = token_store.list_users()
            try:
                presence = {a.agent_id: a for a in bus.agents(timeout_seconds=120)}
            except NotImplementedError:
                presence = {}
            for u in users:
                p = presence.get(u["user_id"])
                u["status"] = p.status if p else "unknown"
                u["last_seen"] = p.last_seen if p else None
            return {"users": users}

        @self._app.post("/api/users")
        def register_user(request: Any) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            data = request.json() if request.body else {}
            user_id = data.get("user_id", "").strip()
            if not user_id:
                return {"error": "Bad Request", "message": "user_id is required"}, 400
            err = _validate_user_id(user_id)
            if err:
                return err
            if token_store.user_exists(user_id):
                return {"error": "Conflict", "message": f"User '{user_id}' already exists"}, 409
            label = data.get("label", "initial token")
            entry = token_store.create_token(agent_id=user_id, label=label)
            return {"ok": True, "user_id": user_id, "token": entry}, 201

    def _setup_user_detail_delete_routes(self) -> None:
        """User detail and deletion routes."""
        token_store = self._token_store
        bus = self._bus

        @self._app.get("/api/users/<user_id>")
        def get_user(request: Any, user_id: str) -> dict | tuple:
            if token_store is None:
                return {"error": "Service Unavailable"}, 503
            tokens = token_store.list_user_tokens(user_id)
            if not tokens:
                return {"error": "Not Found", "message": f"User '{user_id}' not found"}, 404
            try:
                p = bus.agent_status(user_id)
            except NotImplementedError:
                p = None
            return {
                "user_id": user_id,
                "tokens": tokens,
                "status": p.status if p else "unknown",
                "last_seen": p.last_seen if p else None,
            }

        @self._app.delete("/api/users/<user_id>")
        def delete_user(request: Any, user_id: str) -> dict | tuple:
            if token_store is None:
                return {"error": "Service Unavailable"}, 503
            count = token_store.delete_user(user_id)
            if count == 0:
                return {"error": "Not Found", "message": f"User '{user_id}' not found"}, 404
            return {"ok": True, "deleted_tokens": count}

    def _setup_user_token_routes(self) -> None:
        """User token sub-resource routes."""
        token_store = self._token_store

        @self._app.get("/api/users/<user_id>/tokens")
        def list_user_tokens(request: Any, user_id: str) -> dict | tuple:
            if token_store is None:
                return {"error": "Service Unavailable"}, 503
            tokens = token_store.list_user_tokens(user_id)
            return {"user_id": user_id, "tokens": tokens}

        @self._app.post("/api/users/<user_id>/tokens")
        def create_user_token(request: Any, user_id: str) -> dict | tuple:
            if token_store is None:
                return {"error": "Service Unavailable"}, 503
            if not token_store.user_exists(user_id):
                return {"error": "Not Found", "message": f"User '{user_id}' not found"}, 404
            data = request.json() if request.body else {}
            label = data.get("label", "")
            entry = token_store.create_token(agent_id=user_id, label=label)
            return {"ok": True, "token": entry}, 201

    def _setup_token_routes(self) -> None:
        """Token CRUD routes."""
        token_store = self._token_store

        @self._app.get("/api/tokens")
        def list_tokens(request: Any) -> dict:
            if token_store is None:
                return {"tokens": [], "enabled": False}
            return {"tokens": token_store.list_tokens(), "enabled": True}

        @self._app.post("/api/tokens")
        def create_token(request: Any) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            data = request.json() if request.body else {}
            agent_id = data.get("agent_id")
            if not isinstance(agent_id, str) or not agent_id.strip():
                return {
                    "error": "Bad Request",
                    "message": "agent_id must be a non-empty string",
                }, 400
            entry = token_store.create_token(agent_id=agent_id.strip(), label=data.get("label", ""))
            return {"ok": True, "token": entry}, 201

        @self._app.delete("/api/tokens/<token_id>")
        def delete_token(request: Any, token_id: str) -> dict | tuple:
            if token_store is None:
                return {
                    "error": "Service Unavailable",
                    "message": "Token store not configured",
                }, 503
            if token_store.delete_token(token_id):
                return {"ok": True, "deleted": token_id}
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
            return {"ok": True, "token": result}


# ── Helpers ──────────────────────────────────────────────────────


def _extract_session_cookie(cookie_header: str) -> str | None:
    """Extract mansio_session cookie value from a Cookie header string."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{SessionAuth.COOKIE_NAME}="):
            return part[len(SessionAuth.COOKIE_NAME) + 1 :]
    return None


def _msg_dict(m: Any) -> dict:
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
