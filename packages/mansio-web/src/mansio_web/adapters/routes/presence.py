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
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.presence import get as get_presence
from mansio_core.operations.presence import list_presence, touch

from mansio_web._vendor.httpserver import App

from ._common import _bad_request, _error, _json_body, _operation_error

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext

_PRESENCE_UNSUPPORTED = "Backend does not support presence"


def register(app: App, ctx: RouteContext) -> None:
    """Register presence routes."""
    operations = OperationContext(
        bus=ctx.bus,
        authz=ctx.authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )

    @app.post("/v1/presence/heartbeat")
    async def presence_heartbeat(request: Request) -> dict | tuple:
        try:
            heartbeat = parse_heartbeat(_json_body(request, "JSON body required"))
        except ContractError as err:
            return _bad_request(err)

        try:
            await asyncio.to_thread(touch, operations, request.state.principal, heartbeat)
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", _PRESENCE_UNSUPPORTED, 501)

        return PresenceHeartbeatResult().to_dict()

    @app.get("/v1/presence")
    async def presence_list(request: Request) -> dict | tuple:
        try:
            query = parse_presence_list(request.query_params)
        except ContractError as err:
            return _bad_request(err)

        try:
            agents = await asyncio.to_thread(
                list_presence, operations, request.state.principal, query
            )
        except NotImplementedError:
            return _error("Not Implemented", _PRESENCE_UNSUPPORTED, 501)

        return PresenceResult(agents=agents).to_dict()

    @app.get("/v1/presence/<user_id>")
    async def presence_agent(request: Request, user_id: str = "") -> dict | tuple:
        try:
            query = parse_presence_list(request.query_params)
        except ContractError as err:
            return _bad_request(err)

        try:
            result = await asyncio.to_thread(
                get_presence, operations, request.state.principal, user_id, query
            )
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", _PRESENCE_UNSUPPORTED, 501)

        return PresenceAgentResult(
            user_id=result.user_id,
            status=result.status,
            last_seen=result.last_seen,
            metadata=result.metadata,
        ).to_dict()
