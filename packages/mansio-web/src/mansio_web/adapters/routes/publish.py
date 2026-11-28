"""Publish and query routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mansio_core.authz import DM_NAME_EXPECTED, dm_participants
from mansio_core.authz import user_involved as _user_involved
from mansio_core.contracts import (
    ContractError,
    PublishResult,
    QueryResult,
    parse_publish,
    parse_query,
)
from mansio_core.principal import Principal

from mansio_web._vendor.httpserver import App

from ._common import (
    _bad_request,
    _error,
    _invalid_dm_name,
    _json_body,
    _validate_and_auth_publish,
)

if TYPE_CHECKING:
    from mansio_core.types import Message

    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def _require_valid_dm_channel(channel: str) -> None:
    """Raise a contract error for a malformed ``dm:`` channel name."""
    if _invalid_dm_name(channel):
        raise ContractError(DM_NAME_EXPECTED, field="channel")


async def _dm_unregistered_warning(bus: Any, channel: str, sender: str) -> str | None:
    """Return a warning string if a DM targets an unregistered agent."""
    participants = dm_participants(channel)
    if participants is None:
        return None
    other = participants[0] if participants[1] == sender else participants[1]
    try:
        status = await asyncio.to_thread(bus.user_status, other)
    except NotImplementedError:
        return None
    if status is None:
        return f"target agent '{other}' is not registered; message stored but may never be read"
    return None


def _filter_visible_messages(principal: Principal, msgs: list[Message]) -> list[Message]:
    """Filter messages to only those visible to the authenticated agent.

    Supertokens and no-auth mode see all messages.  Scoped tokens
    only see messages where ``_user_involved`` returns True.
    """
    user_id = principal.user_id
    if user_id is None:
        return msgs
    return [m for m in msgs if _user_involved(user_id, m.channel, m.sender)]


def register(app: App, ctx: RouteContext) -> None:
    """Register POST /v1/publish and GET /v1/query."""
    bus = ctx.bus
    authz = ctx.authz
    max_query_limit = ctx.max_query_limit

    @app.post("/v1/publish")
    async def publish(request: Request) -> dict | tuple:
        try:
            payload = parse_publish(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        principal = request.state.principal
        auth_error = _validate_and_auth_publish(
            principal,
            payload.sender,
            payload.channel,
            msg_type=payload.msg_type,
            token_store=ctx.token_store,
            authz=authz,
        )
        if auth_error:
            return auth_error

        try:
            msg_id = await asyncio.to_thread(
                bus.publish,
                channel=payload.channel,
                sender=payload.sender,
                msg_type=payload.msg_type,
                payload=payload.payload,
                metadata=payload.metadata,
                queue=payload.queue,
                parent_id=payload.parent_id,
                intent=payload.intent,
            )
        except ValueError as e:
            status = 404 if "not found" in str(e) else 400
            error = "Not Found" if status == 404 else "Bad Request"
            return _error(error, str(e), status)
        warning = await _dm_unregistered_warning(bus, payload.channel, payload.sender)
        return PublishResult(message_id=msg_id, warning=warning).to_dict()

    @app.get("/v1/query")
    async def query(request: Request) -> dict | tuple:
        try:
            qp = parse_query(
                request.query_params,
                max_limit=max_query_limit,
                validate_channel=_require_valid_dm_channel,
            )
        except ContractError as err:
            return _bad_request(err)

        # Early access check: scoped tokens cannot query private
        # channels they don't belong to.  This also prevents leaking
        # the total message count (issue #251).
        principal = request.state.principal
        user_id = principal.user_id
        if user_id is not None and not authz.can_read(user_id, qp.channel):
            return _error(
                "Forbidden",
                f"Token for '{user_id}' cannot query channel '{qp.channel}'",
                403,
            )

        msgs = await asyncio.to_thread(
            bus.query,
            qp.channel,
            after=qp.after,
            limit=qp.limit,
            msg_type=qp.msg_type,
            order=qp.order,
            thread_id=qp.thread_id,
            intent=qp.intent,
            offset=qp.offset,
        )

        msgs = _filter_visible_messages(principal, msgs)

        total = await asyncio.to_thread(bus.message_count, qp.channel)
        has_more = len(msgs) == qp.limit

        return QueryResult(
            messages=msgs,
            count=len(msgs),
            total=total,
            offset=qp.offset,
            has_more=has_more,
        ).to_dict()
