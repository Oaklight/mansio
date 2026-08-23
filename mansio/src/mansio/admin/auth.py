"""Authentication module for admin panel.

Provides session-cookie-based authentication so the ``Authorization``
header is free for agent Bearer tokens. Static assets (HTML, JS, CSS)
are served without auth so the login overlay can render.

Each login generates a random session token stored in an in-memory set.
Tokens are stored in ``HttpOnly; SameSite=Strict`` cookies. Logout
removes the token from the set, immediately invalidating that session.
Sessions do not survive server restarts (by design — admin sessions
are short-lived).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler

# Login rate limiting: per-IP tracking of failed attempts.
# After MAX_FAILURES consecutive failures, enforce an exponential
# backoff delay before the next attempt is processed.
_MAX_FAILURES = 5
_BASE_DELAY = 1.0  # seconds, doubles each failure beyond MAX_FAILURES
_MAX_DELAY = 30.0  # cap


class SessionAuth:
    """Session-cookie-based admin authentication.

    Each login generates a unique random token tracked in memory.
    Logout invalidates the specific session without affecting others.

    Args:
        password: Admin password. If None, a random 16-char hex
            password is generated.

    Example:
        >>> auth = SessionAuth("my-secret")
        >>> token = auth.create_session()
        >>> auth.validate_session(token)  # True
        >>> auth.revoke_session(token)
        >>> auth.validate_session(token)  # False
    """

    COOKIE_NAME = "mansio_session"

    def __init__(self, password: str | None = None) -> None:
        if password is None:
            self._password = secrets.token_hex(16)
        else:
            self._password = password
        self._sessions: set[str] = set()
        self._lock = threading.Lock()
        # Rate limiting: {client_ip: (failure_count, last_attempt_time)}
        self._login_attempts: dict[str, tuple[int, float]] = {}
        self._rate_lock = threading.Lock()

    @property
    def password(self) -> str:
        """Get the admin password."""
        return self._password

    def create_session(self) -> str:
        """Create a new random session token and track it.

        Returns:
            Random hex string to store in cookie.
        """
        token = secrets.token_hex(32)
        with self._lock:
            self._sessions.add(token)
        return token

    def validate_session(self, session_token: str) -> bool:
        """Validate a session token against the active set.

        Uses constant-time comparison to prevent timing attacks.

        Args:
            session_token: Token from the session cookie.

        Returns:
            True if valid.
        """
        with self._lock:
            # Iterate with constant-time compare to avoid timing leaks
            for active in self._sessions:
                if secrets.compare_digest(session_token, active):
                    return True
        return False

    def revoke_session(self, session_token: str) -> None:
        """Remove a session token from the active set.

        Args:
            session_token: Token to invalidate.
        """
        with self._lock:
            self._sessions.discard(session_token)

    def check_password(self, provided: str) -> bool:
        """Check a password using constant-time comparison.

        Args:
            provided: The password to check.

        Returns:
            True if correct.
        """
        expected_hash = hashlib.sha256(self._password.encode()).digest()
        provided_hash = hashlib.sha256(provided.encode()).digest()
        return secrets.compare_digest(expected_hash, provided_hash)

    def require_auth(self, handler: BaseHTTPRequestHandler) -> bool:
        """Check session cookie and gate API access.

        - ``/api/login``, ``/api/logout``, ``/api/auth-check``: always allowed
        - ``/api/*``: require valid session cookie → 401 if missing
        - Everything else (``/``, static assets): allowed without auth

        Args:
            handler: The HTTP request handler to check.

        Returns:
            True if request should proceed, False if 401 was sent.
        """
        path = handler.path.split("?")[0]

        # Login, logout, and auth-check are always accessible
        if path in ("/api/login", "/api/logout", "/api/auth-check"):
            return True

        # Non-API paths (HTML, static assets) pass through
        if not path.startswith("/api/"):
            return True

        # API paths require valid session cookie
        session_token = self._extract_cookie(handler)
        if session_token and self.validate_session(session_token):
            return True

        self._send_unauthorized(handler, "Admin authentication required")
        return False

    def _check_rate_limit(self, client_ip: str) -> float:
        """Check if a login attempt should be delayed.

        Args:
            client_ip: The client's IP address.

        Returns:
            Seconds to wait (0 if no delay needed).
        """
        with self._rate_lock:
            entry = self._login_attempts.get(client_ip)
            if entry is None:
                return 0
            failures, last_time = entry
            if failures < _MAX_FAILURES:
                return 0
            # Exponential backoff: 1s, 2s, 4s, 8s, ... capped at 30s
            delay = min(_BASE_DELAY * (2 ** (failures - _MAX_FAILURES)), _MAX_DELAY)
            elapsed = time.monotonic() - last_time
            return max(0, delay - elapsed)

    def _record_failure(self, client_ip: str) -> None:
        """Record a failed login attempt."""
        with self._rate_lock:
            entry = self._login_attempts.get(client_ip)
            count = (entry[0] + 1) if entry else 1
            self._login_attempts[client_ip] = (count, time.monotonic())

    def _clear_failures(self, client_ip: str) -> None:
        """Clear failure count on successful login."""
        with self._rate_lock:
            self._login_attempts.pop(client_ip, None)

    def handle_login(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        """Handle ``POST /api/login`` — validate password, set cookie.

        Enforces per-IP rate limiting after repeated failures:
        exponential backoff from 1s to 30s after 5 consecutive failures.

        Args:
            handler: The HTTP request handler.
            body: Raw request body (JSON with ``password`` field).
        """
        client_ip = handler.client_address[0]

        # Rate limit check
        wait = self._check_rate_limit(client_ip)
        if wait > 0:
            self._send_json(
                handler,
                {"error": "Too many attempts", "retry_after": round(wait, 1)},
                429,
            )
            return

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_json(handler, {"error": "Invalid JSON"}, 400)
            return

        password = data.get("password", "")
        if not self.check_password(password):
            self._record_failure(client_ip)
            self._send_json(handler, {"error": "Invalid password"}, 401)
            return

        self._clear_failures(client_ip)
        session_token = self.create_session()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header(
            "Set-Cookie",
            f"{self.COOKIE_NAME}={session_token}; HttpOnly; SameSite=Strict; Path=/",
        )
        _send_cors(handler)
        handler.end_headers()
        handler.wfile.write(json.dumps({"ok": True}).encode())

    def handle_auth_check(self, handler: BaseHTTPRequestHandler) -> None:
        """Handle ``GET /api/auth-check`` — return auth status.

        Args:
            handler: The HTTP request handler.
        """
        session_token = self._extract_cookie(handler)
        authenticated = bool(session_token and self.validate_session(session_token))
        self._send_json(handler, {"authenticated": authenticated, "required": True})

    def handle_logout(self, handler: BaseHTTPRequestHandler) -> None:
        """Handle ``POST /api/logout`` — revoke session and clear cookie.

        Args:
            handler: The HTTP request handler.
        """
        # Revoke the session server-side so the token is immediately invalid
        session_token = self._extract_cookie(handler)
        if session_token:
            self.revoke_session(session_token)

        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header(
            "Set-Cookie",
            f"{self.COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
        )
        _send_cors(handler)
        handler.end_headers()
        handler.wfile.write(json.dumps({"ok": True}).encode())

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _extract_cookie(handler: BaseHTTPRequestHandler) -> str | None:
        """Extract session cookie value from request headers."""
        cookie_header = handler.headers.get("Cookie", "")
        if not cookie_header:
            return None
        try:
            c = cookies.SimpleCookie(cookie_header)
            morsel = c.get(SessionAuth.COOKIE_NAME)
            return morsel.value if morsel else None
        except cookies.CookieError:
            return None

    @staticmethod
    def _send_json(handler: BaseHTTPRequestHandler, data: dict, status: int = 200) -> None:
        """Send a JSON response."""
        body = json.dumps(data).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        _send_cors(handler)
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _send_unauthorized(handler: BaseHTTPRequestHandler, message: str) -> None:
        """Send a 401 Unauthorized response."""
        body = json.dumps({"error": "Unauthorized", "message": message}).encode()
        handler.send_response(401)
        handler.send_header("Content-Type", "application/json")
        _send_cors(handler)
        handler.end_headers()
        handler.wfile.write(body)


# Keep backward-compatible name for imports (deprecated)
TokenAuth = SessionAuth


def _send_cors(handler: BaseHTTPRequestHandler) -> None:
    """Send CORS headers. SameSite=Strict cookies handle cross-origin
    protection, so Allow-Credentials is not needed."""
    handler.send_header("Access-Control-Allow-Origin", "*")
