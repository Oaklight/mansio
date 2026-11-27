"""Admin UI shell and asset routes."""

from __future__ import annotations

from typing import Any

from mansio_web._vendor.httpserver import App, Response

from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register the root page and the whitelisted UI assets."""
    serve_ui = ctx.serve_ui

    @app.get("/assets/<path:path>")
    def asset(request: Any, path: str) -> Response | tuple:
        from ..static import get_asset

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

    @app.get("/")
    def root(request: Any) -> Response | dict:
        if serve_ui:
            from ..static import ADMIN_HTML

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
