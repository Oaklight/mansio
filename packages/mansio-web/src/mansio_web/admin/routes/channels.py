"""Channel listing and detail routes."""

from __future__ import annotations

from typing import Any

from mansio_core.operations import OperationError

from mansio_web._vendor.httpserver import App

from . import RouteContext, operation_error


def register(app: App, ctx: RouteContext) -> None:
    """Register channel reads."""
    view = ctx.view

    @app.get("/api/channels")
    def channels(request: Any) -> dict | tuple:
        try:
            return view.channels(request.state.principal).to_dict()
        except OperationError as err:
            return operation_error(err)

    @app.get("/api/channels/<name>")
    def channel_detail(request: Any, name: str) -> dict | tuple:
        try:
            detail = view.channel_detail(request.state.principal, name)
        except OperationError as err:
            return operation_error(err)
        if detail is None:
            return {"error": "Not Found", "message": f"Channel '{name}' not found"}, 404
        return detail.to_dict()
