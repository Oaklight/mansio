"""Channel listing route."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mansio_core.contracts import ChannelsResult, parse_channels
from mansio_core.operations import OperationContext
from mansio_core.operations.channels import list_channels

from mansio_web._vendor.httpserver import App

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register GET /v1/channels."""
    operations = OperationContext(
        bus=ctx.bus,
        authz=ctx.authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )

    @app.get("/v1/channels")
    async def channels(request: Request) -> dict | tuple:
        want_detail = parse_channels(request.query_params).detail
        channels_out = await asyncio.to_thread(
            list_channels, operations, request.state.principal, want_detail
        )
        return ChannelsResult(channels=channels_out).to_dict()
