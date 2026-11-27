"""Session login, logout and auth-check routes."""

from __future__ import annotations

from typing import Any

from mansio_web._vendor.httpserver import App, JSONResponse, Response

from ..auth import SessionAuth
from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register the session routes."""
    auth = ctx.auth

    @app.get("/api/auth-check")
    def auth_check(request: Any) -> dict:
        if auth is None:
            return {"authenticated": True, "required": False}
        session_token = request.cookies.get(SessionAuth.COOKIE_NAME)
        authenticated = bool(session_token and auth.validate_session(session_token))
        return {"authenticated": authenticated, "required": True}

    @app.post("/api/login")
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

    @app.post("/api/logout")
    def logout(request: Any) -> Response:
        if auth:
            session_token = request.cookies.get(SessionAuth.COOKIE_NAME)
            if session_token:
                auth.revoke_session(session_token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SessionAuth.COOKIE_NAME, path="/")
        return resp
