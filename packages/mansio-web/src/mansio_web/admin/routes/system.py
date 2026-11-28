"""System information route."""

from __future__ import annotations

from typing import Any

from mansio_core.operations import OperationError

from mansio_web._vendor.httpserver import App

from . import RouteContext, operation_error


def register(app: App, ctx: RouteContext) -> None:
    """Register the system info route."""
    view = ctx.view

    @app.get("/api/system")
    def system_info(request: Any) -> dict | tuple:
        try:
            return view.system(request.state.principal).to_dict()
        except OperationError as err:
            return operation_error(err)
