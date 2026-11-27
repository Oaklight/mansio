"""Subscription listing route."""

from __future__ import annotations

from typing import Any

from mansio_web._vendor.httpserver import App

from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register the subscription list."""
    view = ctx.view

    @app.get("/api/subscriptions")
    def subscriptions(request: Any) -> dict:
        return view.subscriptions().to_dict()
