"""Message and channel deletion routes, including admin maintenance."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mansio_core.contracts import (
    ContractError,
    DeleteChannelResult,
    DeleteMessageResult,
    parse_admin_cleanup,
    parse_admin_compact,
)
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.messages import DeleteMessageRequest, require_privileged
from mansio_core.operations.messages import cleanup as cleanup_channels
from mansio_core.operations.messages import compact as compact_channel
from mansio_core.operations.messages import delete_message as delete_message_operation

from mansio_web._vendor.httpserver import App

from ._common import _bad_request, _error, _json_body, _operation_error

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register deletion and admin routes."""
    _register_delete_routes(app, ctx)
    _register_admin_routes(app, ctx)


def _register_delete_routes(app: App, ctx: RouteContext) -> None:
    bus = ctx.bus
    authz = ctx.authz
    operations = OperationContext(
        bus=bus,
        authz=authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )

    @app.delete("/v1/channels/<channel_name>")
    async def delete_channel(request: Request, channel_name: str = "") -> dict | tuple:
        if not channel_name:
            return _error("Bad Request", "Channel name required", 400)

        user_id = request.state.principal.user_id
        if user_id is not None:
            denial = authz.delete_channel_denial(user_id, channel_name)
            if denial:
                return _error("Forbidden", denial, 403)

        try:
            count = await asyncio.to_thread(bus.delete_channel, channel_name)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support deletion", 501)

        if count == 0:
            return _error("Not Found", f"Channel '{channel_name}' not found or already empty", 404)

        return DeleteChannelResult(deleted=count, channel=channel_name).to_dict()

    @app.delete("/v1/messages/<message_id>")
    async def delete_message(request: Request, message_id: str = "") -> dict | tuple:
        if not message_id:
            return _error("Bad Request", "Message ID required", 400)

        try:
            await asyncio.to_thread(
                delete_message_operation,
                operations,
                request.state.principal,
                DeleteMessageRequest(message_id),
            )
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support deletion", 501)

        return DeleteMessageResult(deleted=True, message_id=message_id).to_dict()


def _register_admin_routes(app: App, ctx: RouteContext) -> None:
    _register_admin_cleanup(app, ctx)
    _register_admin_compact(app, ctx)


def _register_admin_cleanup(app: App, ctx: RouteContext) -> None:
    operations = OperationContext(
        bus=ctx.bus,
        authz=ctx.authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )

    @app.post("/v1/admin/channels/cleanup")
    async def admin_cleanup(request: Request) -> dict | tuple:
        principal = request.state.principal
        # The gate runs before body parsing so a forbidden caller still gets
        # 403 for a malformed body, as it did when the check lived here.
        try:
            require_privileged(principal)
        except OperationError as err:
            return _operation_error(err)

        try:
            cleanup = parse_admin_cleanup(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        try:
            result = await asyncio.to_thread(cleanup_channels, operations, principal, cleanup)
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support deletion", 501)

        return result.to_dict()


def _register_admin_compact(app: App, ctx: RouteContext) -> None:
    operations = OperationContext(
        bus=ctx.bus,
        authz=ctx.authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )

    @app.post("/v1/admin/compact")
    async def admin_compact(request: Request) -> dict | tuple:
        principal = request.state.principal
        try:
            require_privileged(principal)
        except OperationError as err:
            return _operation_error(err)

        try:
            compact = parse_admin_compact(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        try:
            result = await asyncio.to_thread(compact_channel, operations, principal, compact)
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support compaction", 501)

        return result.to_dict()
