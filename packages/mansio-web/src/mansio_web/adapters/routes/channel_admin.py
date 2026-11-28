"""Channel creation, metadata, and ACL management routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mansio_core.contracts import (
    ACLAddEntry,
    ACLAddResult,
    ACLRemoveResult,
    ACLResult,
    ACLSetResult,
    ChannelResult,
    ContractError,
    parse_acl_add,
    parse_acl_set,
    parse_channel_create,
)
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.acl import add_acl_entry as add_acl_entry_operation
from mansio_core.operations.acl import get_acl as get_acl_operation
from mansio_core.operations.acl import remove_acl_entry as remove_acl_entry_operation
from mansio_core.operations.acl import require_manage
from mansio_core.operations.acl import set_acl as set_acl_operation
from mansio_core.operations.channels import channel_meta as channel_meta_operation
from mansio_core.operations.channels import create_channel as create_channel_operation

from mansio_web._vendor.httpserver import App

from ._common import _bad_request, _error, _json_body, _operation_error

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext

_CHANNEL_MANAGEMENT_UNSUPPORTED = "Backend does not support channel management"


def _operations(ctx: RouteContext) -> OperationContext:
    return OperationContext(
        bus=ctx.bus,
        authz=ctx.authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )


def register(app: App, ctx: RouteContext) -> None:
    """Register channel creation, metadata, and ACL routes."""
    _register_channel_routes(app, ctx)
    _register_acl_routes(app, ctx)


def _register_channel_routes(app: App, ctx: RouteContext) -> None:
    operations = _operations(ctx)

    @app.post("/v1/channels")
    async def create_channel(request: Request) -> dict | tuple:
        try:
            payload = parse_channel_create(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        try:
            meta = await asyncio.to_thread(
                create_channel_operation, operations, request.state.principal, payload
            )
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", _CHANNEL_MANAGEMENT_UNSUPPORTED, 501)

        return ChannelResult(channel=meta).to_dict(), 201

    @app.get("/v1/channels/<channel>/meta")
    async def get_channel_meta(request: Request, channel: str = "") -> dict | tuple:
        try:
            meta = await asyncio.to_thread(
                channel_meta_operation, operations, request.state.principal, channel
            )
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", _CHANNEL_MANAGEMENT_UNSUPPORTED, 501)

        return ChannelResult(channel=meta).to_dict()


def _register_acl_routes(app: App, ctx: RouteContext) -> None:
    _register_acl_read(app, ctx)
    _register_acl_write(app, ctx)


def _register_acl_read(app: App, ctx: RouteContext) -> None:
    operations = _operations(ctx)

    @app.get("/v1/channels/<channel>/acl")
    async def get_acl(request: Request, channel: str = "") -> dict | tuple:
        try:
            entries = await asyncio.to_thread(
                get_acl_operation, operations, request.state.principal, channel
            )
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", _CHANNEL_MANAGEMENT_UNSUPPORTED, 501)

        return ACLResult(acl=entries).to_dict()


def _register_acl_write(app: App, ctx: RouteContext) -> None:
    _register_acl_set(app, ctx)
    _register_acl_add(app, ctx)
    _register_acl_remove(app, ctx)


def _register_acl_set(app: App, ctx: RouteContext) -> None:
    operations = _operations(ctx)

    @app.put("/v1/channels/<channel>/acl")
    async def set_acl(request: Request, channel: str = "") -> dict | tuple:
        principal = request.state.principal
        # The manage gate runs before body parsing so a denied caller still
        # gets 403 for a malformed body, as it did when the check lived here.
        # It may reach the backend, so it runs off the event loop.
        try:
            await asyncio.to_thread(require_manage, operations, principal, channel)
        except OperationError as err:
            return _operation_error(err)

        try:
            payload = parse_acl_set(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        try:
            entries = await asyncio.to_thread(
                set_acl_operation, operations, principal, channel, payload
            )
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", _CHANNEL_MANAGEMENT_UNSUPPORTED, 501)

        return ACLSetResult(status="ok", count=len(entries)).to_dict()


def _register_acl_add(app: App, ctx: RouteContext) -> None:
    operations = _operations(ctx)

    @app.post("/v1/channels/<channel>/acl")
    async def add_acl_entry(request: Request, channel: str = "") -> dict | tuple:
        principal = request.state.principal
        try:
            await asyncio.to_thread(require_manage, operations, principal, channel)
        except OperationError as err:
            return _operation_error(err)

        try:
            payload = parse_acl_add(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        try:
            entry = await asyncio.to_thread(
                add_acl_entry_operation, operations, principal, channel, payload
            )
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", _CHANNEL_MANAGEMENT_UNSUPPORTED, 501)

        return (
            ACLAddResult(
                status="ok",
                entry=ACLAddEntry(
                    channel=entry.channel,
                    user_id=entry.user_id,
                    permission=entry.permission,
                ),
            ).to_dict(),
            201,
        )


def _register_acl_remove(app: App, ctx: RouteContext) -> None:
    operations = _operations(ctx)

    @app.delete("/v1/channels/<channel>/acl/<user_id>")
    async def remove_acl_entry(
        request: Request, channel: str = "", user_id: str = ""
    ) -> dict | tuple:
        try:
            await asyncio.to_thread(
                remove_acl_entry_operation,
                operations,
                request.state.principal,
                channel,
                user_id,
            )
        except OperationError as err:
            return _operation_error(err)
        except NotImplementedError:
            return _error("Not Implemented", _CHANNEL_MANAGEMENT_UNSUPPORTED, 501)

        return ACLRemoveResult().to_dict()
