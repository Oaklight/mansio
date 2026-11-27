"""Queue claim, ack, and status routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mansio_core.contracts import (
    ContractError,
    QueueAckResult,
    QueueClaimResult,
    QueueStatusResult,
    parse_queue_ack,
    parse_queue_claim,
    parse_queue_status,
)

from mansio_web._vendor.httpserver import App

from ._common import _bad_request, _error, _json_body

if TYPE_CHECKING:
    from mansio_core.authz import ChannelAuthorizer

    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


async def _handle_queue_status(request: Any, bus: Any, authz: ChannelAuthorizer) -> dict | tuple:
    """Handle /v1/queue/status requests."""
    try:
        status = parse_queue_status(request.query_params)
    except ContractError as err:
        return _bad_request(err)
    message_id = status.message_id

    auth_result = request.state.auth_result
    if isinstance(auth_result, str):
        message = await asyncio.to_thread(bus.get_message, message_id)
        if message is not None and not authz.can_read(auth_result, message.channel):
            # Same body as a message without queue state, so the answer does
            # not confirm that a private message exists.
            return _error("Not Found", "No queue status for this message", 404)

    result = await asyncio.to_thread(bus.queue_status, message_id)
    if result is None:
        return _error("Not Found", "No queue status for this message", 404)
    return QueueStatusResult(found=True, status=result).to_dict()


def _resolve_claimed_by(auth_result: Any, claimed_by: str | None) -> tuple[str, tuple | None]:
    """Resolve claimed_by from the auth context or the parsed request body."""
    if auth_result is False:
        return "", (_error("Unauthorized", "Valid token required", 401))
    if isinstance(auth_result, str):
        return auth_result, None
    if not claimed_by:
        return "", (_error("Bad Request", "Missing 'claimed_by'", 400))
    return claimed_by, None


def register(app: App, ctx: RouteContext) -> None:
    """Register queue routes."""
    bus = ctx.bus
    authz = ctx.authz

    # ── Shared handlers ───────────────────────────────────────

    async def _handle_claim(request: Request) -> dict | tuple:
        try:
            claim = parse_queue_claim(_json_body(request, "Invalid JSON"))
        except ContractError as err:
            return _bad_request(err)
        auth_result = request.state.auth_result
        claimed_by, err = _resolve_claimed_by(auth_result, claim.claimed_by)
        if err:
            return err

        if isinstance(auth_result, str):
            denial = await asyncio.to_thread(authz.claim_denial, auth_result, claim.channel)
            if denial:
                return _error("Forbidden", denial, 403)

        result = await asyncio.to_thread(
            bus.queue_claim, claim.channel, claimed_by, lease_seconds=claim.lease_seconds
        )
        if result is None:
            return QueueClaimResult(claimed=False).to_dict()
        return QueueClaimResult(claimed=True, result=result).to_dict()

    async def _handle_ack(request: Request) -> dict | tuple:
        try:
            ack = parse_queue_ack(_json_body(request, "Invalid JSON"))
        except ContractError as err:
            return _bad_request(err)
        auth_result = request.state.auth_result
        claimed_by, err = _resolve_claimed_by(auth_result, ack.claimed_by)
        if err:
            return err

        if isinstance(auth_result, str):
            message = await asyncio.to_thread(bus.get_message, ack.message_id)
            if message is not None and not authz.can_read(auth_result, message.channel):
                # Same answer as "not claimed by you": an unauthorized
                # caller learns nothing about the message's existence.
                return QueueAckResult(acked=False).to_dict()

        result = await asyncio.to_thread(bus.queue_ack, ack.message_id, claimed_by)
        if result is None:
            return QueueAckResult(acked=False).to_dict()
        return QueueAckResult(acked=True, result=result).to_dict()

    # ── Canonical paths (/v1/queue/*) ─────────────────────────

    @app.post("/v1/queue/claim")
    async def queue_claim(request: Request) -> dict | tuple:
        return await _handle_claim(request)

    @app.post("/v1/queue/ack")
    async def queue_ack(request: Request) -> dict | tuple:
        return await _handle_ack(request)

    @app.get("/v1/queue/status")
    async def queue_status(request: Request) -> dict | tuple:
        return await _handle_queue_status(request, bus, authz)
