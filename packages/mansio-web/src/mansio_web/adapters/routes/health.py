"""Health check route."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mansio_core.contracts import HealthResult

from mansio_web._vendor.httpserver import App

if TYPE_CHECKING:
    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register GET /health."""

    @app.get("/health")
    async def health(request: Any) -> dict:
        return HealthResult().to_dict()
