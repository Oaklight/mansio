"""System information route."""

from __future__ import annotations

from typing import Any

from mansio_web._vendor.httpserver import App

from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register the system info route."""
    view = ctx.view

    @app.get("/api/system")
    def system_info(request: Any) -> dict:
        return view.system().to_dict()
