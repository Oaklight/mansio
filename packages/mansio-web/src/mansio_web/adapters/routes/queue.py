"""Queue claim, ack, and status routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mansio_core.contracts import (
    ContractError,
    QueueAckResult,
    QueueClaimResult,
    QueueStatusResult,
    parse_queue_ack,
    parse_queue_claim,
    parse_queue_status,
)
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.queue import ack as ack_message
from mansio_core.operations.queue import claim as claim_message
from mansio_core.operations.queue import status as queue_status_operation

from mansio_web._vendor.httpserver import App

from ._common import _bad_request, _error, _json_body, _operation_error

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register queue routes."""
    operations = OperationContext(
        bus=ctx.bus,
        authz=ctx.authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )

    # ── Shared handlers ───────────────────────────────────────

    async def _handle_claim(request: Request) -> dict | tuple:
        try:
            claim = parse_queue_claim(_json_body(request, "Invalid JSON"))
        except ContractError as err:
            return _bad_request(err)

        try:
            result = await asyncio.to_thread(
                claim_message, operations, request.state.principal, claim
            )
        except OperationError as err:
            return _operation_error(err)

        if result is None:
            return QueueClaimResult(claimed=False).to_dict()
        return QueueClaimResult(claimed=True, result=result).to_dict()

    async def _handle_ack(request: Request) -> dict | tuple:
        try:
            ack = parse_queue_ack(_json_body(request, "Invalid JSON"))
        except ContractError as err:
            return _bad_request(err)

        try:
            result = await asyncio.to_thread(ack_message, operations, request.state.principal, ack)
        except OperationError as err:
            return _operation_error(err)

        if result is None:
            return QueueAckResult(acked=False).to_dict()
        return QueueAckResult(acked=True, result=result).to_dict()

    async def _handle_queue_status(request: Request) -> dict | tuple:
        try:
            status = parse_queue_status(request.query_params)
        except ContractError as err:
            return _bad_request(err)

        try:
            result = await asyncio.to_thread(
                queue_status_operation, operations, request.state.principal, status
            )
        except OperationError as err:
            return _operation_error(err)

        if result is None:
            return _error("Not Found", "No queue status for this message", 404)
        return QueueStatusResult(found=True, status=result).to_dict()

    # ── Canonical paths (/v1/queue/*) ─────────────────────────

    @app.post("/v1/queue/claim")
    async def queue_claim(request: Request) -> dict | tuple:
        return await _handle_claim(request)

    @app.post("/v1/queue/ack")
    async def queue_ack(request: Request) -> dict | tuple:
        return await _handle_ack(request)

    @app.get("/v1/queue/status")
    async def queue_status(request: Request) -> dict | tuple:
        return await _handle_queue_status(request)
