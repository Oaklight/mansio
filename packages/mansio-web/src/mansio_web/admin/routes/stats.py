"""Dashboard statistics routes."""

from __future__ import annotations

from typing import Any

from mansio_web._vendor.httpserver import App

from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register stats and throughput."""
    view = ctx.view

    @app.get("/api/stats")
    def stats(request: Any) -> dict:
        return view.stats().to_dict()

    @app.get("/api/stats/throughput")
    def throughput(request: Any) -> dict:
        return view.throughput().to_dict()
