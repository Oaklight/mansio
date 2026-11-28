"""Channel creation, metadata, and ACL management routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from mansio_core.authz import DM_PREFIX, ChannelAuthorizer
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
from mansio_core.principal import Principal
from mansio_core.types import ACLEntry

from mansio_web._vendor.httpserver import App

from ._common import _bad_request, _error, _json_body, _validate_channel_name

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


async def _require_acl_manage(
    principal: Principal, authz: ChannelAuthorizer, channel: str
) -> tuple[dict, int] | None:
    """Check that the current user may manage *channel*.

    Returns an error tuple (dict, status) if denied, or None if allowed.
    Supertokens and no-auth mode always pass.
    """
    user_id = principal.user_id
    if user_id is not None and not await asyncio.to_thread(authz.can_manage_acl, user_id, channel):
        return _error("Forbidden", f"Agent '{user_id}' lacks manage permission on '{channel}'", 403)
    return None


def register(app: App, ctx: RouteContext) -> None:
    """Register channel creation, metadata, and ACL routes."""
    _register_channel_routes(app, ctx)
    _register_acl_routes(app, ctx)


def _register_channel_routes(app: App, ctx: RouteContext) -> None:
    bus = ctx.bus
    authz = ctx.authz
    token_store = ctx.token_store

    @app.post("/v1/channels")
    async def create_channel(request: Request) -> dict | tuple:
        try:
            payload = parse_channel_create(_json_body(request))
        except ContractError as err:
            return _bad_request(err)
        name = payload.name

        if name.startswith(DM_PREFIX):
            error = _validate_channel_name(name, token_store=token_store)
            if error:
                return error

        principal = request.state.principal
        owner = payload.owner or ""
        user_id = principal.user_id
        if user_id is not None:
            denial = authz.create_denial(user_id, name)
            if denial:
                return _error("Forbidden", denial, 403)
            owner = user_id  # scoped tokens own what they create
        elif not owner:
            return _error("Bad Request", "'owner' is required for supertoken/no-auth", 400)

        visibility = payload.visibility

        try:
            meta = await asyncio.to_thread(bus.create_channel, name, owner, visibility=visibility)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support channel management", 501)
        except ValueError as exc:
            return _error("Conflict", str(exc), 409)

        return ChannelResult(channel=meta).to_dict(), 201

    @app.get("/v1/channels/<channel>/meta")
    async def get_channel_meta(request: Request, channel: str = "") -> dict | tuple:
        user_id = request.state.principal.user_id
        if user_id is not None and not authz.can_read(user_id, channel):
            # 404, not 403: a denied caller must not learn from the
            # status code that a private channel exists.  The message is
            # the same one an unknown channel returns.
            return _error("Not Found", f"Channel '{channel}' not found", 404)

        try:
            meta = await asyncio.to_thread(bus.get_channel_meta, channel)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support channel management", 501)

        if meta is None:
            return _error("Not Found", f"Channel '{channel}' not found", 404)

        return ChannelResult(channel=meta).to_dict()


def _register_acl_routes(app: App, ctx: RouteContext) -> None:
    _register_acl_read(app, ctx)
    _register_acl_write(app, ctx)


def _register_acl_read(app: App, ctx: RouteContext) -> None:
    bus = ctx.bus
    authz = ctx.authz

    @app.get("/v1/channels/<channel>/acl")
    async def get_acl(request: Request, channel: str = "") -> dict | tuple:
        principal = request.state.principal
        denied = await _require_acl_manage(principal, authz, channel)
        if denied:
            return denied

        try:
            entries = await asyncio.to_thread(bus.get_acl, channel)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support channel management", 501)

        return ACLResult(acl=entries).to_dict()


def _register_acl_write(app: App, ctx: RouteContext) -> None:
    _register_acl_set(app, ctx)
    _register_acl_add(app, ctx)
    _register_acl_remove(app, ctx)


def _register_acl_set(app: App, ctx: RouteContext) -> None:
    bus = ctx.bus
    authz = ctx.authz

    @app.put("/v1/channels/<channel>/acl")
    async def set_acl(request: Request, channel: str = "") -> dict | tuple:
        principal = request.state.principal
        denied = await _require_acl_manage(principal, authz, channel)
        if denied:
            return denied

        try:
            payload = parse_acl_set(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        now = _now_iso()
        granted_by = principal.user_id
        entries = [
            ACLEntry(
                channel=channel,
                user_id=spec.user_id,
                permission=spec.permission,
                granted_at=now,
                granted_by=granted_by,
            )
            for spec in payload.entries
        ]

        try:
            if await asyncio.to_thread(bus.get_channel_meta, channel) is None:
                return _error("Not Found", f"Channel '{channel}' not found", 404)
            await asyncio.to_thread(bus.set_acl, channel, entries)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support channel management", 501)

        return ACLSetResult(status="ok", count=len(entries)).to_dict()


def _register_acl_add(app: App, ctx: RouteContext) -> None:
    bus = ctx.bus
    authz = ctx.authz

    @app.post("/v1/channels/<channel>/acl")
    async def add_acl_entry(request: Request, channel: str = "") -> dict | tuple:
        principal = request.state.principal
        denied = await _require_acl_manage(principal, authz, channel)
        if denied:
            return denied

        try:
            payload = parse_acl_add(_json_body(request))
        except ContractError as err:
            return _bad_request(err)
        user_id = payload.user_id
        permission = payload.permission

        now = _now_iso()
        granted_by = principal.user_id
        entry = ACLEntry(
            channel=channel,
            user_id=user_id,
            permission=permission,
            granted_at=now,
            granted_by=granted_by,
        )

        try:
            if await asyncio.to_thread(bus.get_channel_meta, channel) is None:
                return _error("Not Found", f"Channel '{channel}' not found", 404)
            await asyncio.to_thread(bus.add_acl_entry, entry)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support channel management", 501)

        return ACLAddResult(
            status="ok",
            entry=ACLAddEntry(
                channel=entry.channel,
                user_id=entry.user_id,
                permission=entry.permission,
            ),
        ).to_dict(), 201


def _register_acl_remove(app: App, ctx: RouteContext) -> None:
    bus = ctx.bus
    authz = ctx.authz

    @app.delete("/v1/channels/<channel>/acl/<user_id>")
    async def remove_acl_entry(
        request: Request, channel: str = "", user_id: str = ""
    ) -> dict | tuple:
        principal = request.state.principal
        denied = await _require_acl_manage(principal, authz, channel)
        if denied:
            return denied

        try:
            removed = await asyncio.to_thread(bus.remove_acl_entry, channel, user_id)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support channel management", 501)

        if not removed:
            return _error("Not Found", f"No ACL entry for '{user_id}' on '{channel}'", 404)

        return ACLRemoveResult().to_dict()
