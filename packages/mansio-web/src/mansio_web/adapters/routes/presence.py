"""Presence heartbeat and roster routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mansio_core.contracts import (
    ContractError,
    PresenceAgentResult,
    PresenceHeartbeatResult,
    PresenceResult,
    parse_heartbeat,
    parse_presence_list,
)

from mansio_web._vendor.httpserver import App

from ._common import _bad_request, _error, _json_body

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register presence routes."""
    bus = ctx.bus

    @app.post("/v1/presence/heartbeat")
    async def presence_heartbeat(request: Request) -> dict | tuple:
        auth_result = request.state.auth_result
        try:
            heartbeat = parse_heartbeat(_json_body(request, "JSON body required"))
        except ContractError as err:
            return _bad_request(err)
        user_id = heartbeat.user_id
        # A scoped token may only report its own user's presence.
        # Supertokens (auth_result is None) and no-auth mode (True)
        # keep acting on behalf of any user, as they do elsewhere.
        if isinstance(auth_result, str) and user_id != auth_result:
            return _error(
                "Forbidden",
                f"Token bound to '{auth_result}', cannot heartbeat as '{user_id}'",
                403,
            )
        metadata = heartbeat.metadata
        try:
            await asyncio.to_thread(bus.heartbeat, user_id, metadata)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support presence", 501)
        return PresenceHeartbeatResult().to_dict()

    @app.get("/v1/presence")
    async def presence_list(request: Request) -> dict | tuple:
        try:
            query = parse_presence_list(request.query_params)
        except ContractError as err:
            return _bad_request(err)
        try:
            agents = await asyncio.to_thread(bus.users, query.timeout)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support presence", 501)
        return PresenceResult(agents=agents).to_dict()

    @app.get("/v1/presence/<user_id>")
    async def presence_agent(request: Request, user_id: str = "") -> dict | tuple:
        try:
            query = parse_presence_list(request.query_params)
        except ContractError as err:
            return _bad_request(err)
        try:
            result = await asyncio.to_thread(bus.user_status, user_id, query.timeout)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support presence", 501)
        if result is None:
            return _error("Not Found", "agent not found", 404)
        return PresenceAgentResult(
            user_id=result.user_id,
            status=result.status,
            last_seen=result.last_seen,
            metadata=result.metadata,
        ).to_dict()
