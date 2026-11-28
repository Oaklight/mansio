"""Subscription listing route."""

from __future__ import annotations

from typing import Any

from mansio_core.operations import OperationError

from mansio_web._vendor.httpserver import App

from . import RouteContext, operation_error


def register(app: App, ctx: RouteContext) -> None:
    """Register the subscription list."""
    view = ctx.view

    @app.get("/api/subscriptions")
    def subscriptions(request: Any) -> dict | tuple:
        try:
            return view.subscriptions(request.state.principal).to_dict()
        except OperationError as err:
            return operation_error(err)
