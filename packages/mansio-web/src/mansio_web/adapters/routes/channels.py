"""Channel listing route."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from mansio_core.contracts import ChannelsResult, parse_channels

from mansio_web._vendor.httpserver import App

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register GET /v1/channels."""
    bus = ctx.bus
    authz = ctx.authz

    def _visible_channels_detail(user_id: str) -> list[dict]:
        """List channel detail for one user, filtered in a single thread hop."""
        detail = bus.channels_detail()
        allowed = authz.can_list_channels(user_id, [ch["name"] for ch in detail])
        return [ch for ch in detail if allowed[ch["name"]]]

    def _visible_channel_names(user_id: str) -> list[str]:
        """List channel names for one user, filtered in a single thread hop."""
        names = cast(list[str], bus.channels(detail=False))
        allowed = authz.can_list_channels(user_id, names)
        return [name for name in names if allowed[name]]

    @app.get("/v1/channels")
    async def channels(request: Request) -> dict | tuple:
        want_detail = parse_channels(request.query_params).detail

        user_id = request.state.principal.user_id

        if want_detail:
            if user_id is not None:
                all_detail = await asyncio.to_thread(_visible_channels_detail, user_id)
            else:
                all_detail = await asyncio.to_thread(bus.channels_detail)
            return ChannelsResult(channels=all_detail).to_dict()

        if user_id is not None:
            all_channels = await asyncio.to_thread(_visible_channel_names, user_id)
        else:
            all_channels = cast(list[str], await asyncio.to_thread(bus.channels, detail=False))
        return ChannelsResult(channels=all_channels).to_dict()
