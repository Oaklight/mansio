"""Dashboard statistics routes."""

from __future__ import annotations

from typing import Any

from mansio_core.operations import OperationError

from mansio_web._vendor.httpserver import App

from . import RouteContext, operation_error


def register(app: App, ctx: RouteContext) -> None:
    """Register stats and throughput."""
    view = ctx.view

    @app.get("/api/stats")
    def stats(request: Any) -> dict | tuple:
        try:
            return view.stats(request.state.principal).to_dict()
        except OperationError as err:
            return operation_error(err)

    @app.get("/api/stats/throughput")
    def throughput(request: Any) -> dict | tuple:
        try:
            return view.throughput(request.state.principal).to_dict()
        except OperationError as err:
            return operation_error(err)
