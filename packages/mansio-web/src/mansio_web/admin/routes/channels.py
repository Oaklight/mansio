"""Channel listing and detail routes."""

from __future__ import annotations

from typing import Any

from mansio_web._vendor.httpserver import App

from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register channel reads."""
    view = ctx.view

    @app.get("/api/channels")
    def channels(request: Any) -> dict:
        return view.channels().to_dict()

    @app.get("/api/channels/<name>")
    def channel_detail(request: Any, name: str) -> dict | tuple:
        detail = view.channel_detail(name)
        if detail is None:
            return {"error": "Not Found", "message": f"Channel '{name}' not found"}, 404
        return detail.to_dict()
