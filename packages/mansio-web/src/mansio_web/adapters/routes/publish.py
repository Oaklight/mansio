"""Publish and query routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mansio_core.contracts import (
    ContractError,
    PublishResult,
    QueryResult,
    parse_publish,
    parse_query,
)
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.messages import publish as publish_message
from mansio_core.operations.messages import query as query_messages

from mansio_web._vendor.httpserver import App

from ._common import _bad_request, _json_body, _operation_error

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register POST /v1/publish and GET /v1/query."""
    max_query_limit = ctx.max_query_limit
    operations = OperationContext(
        bus=ctx.bus,
        authz=ctx.authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )

    @app.post("/v1/publish")
    async def publish(request: Request) -> dict | tuple:
        try:
            payload = parse_publish(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        try:
            outcome = await asyncio.to_thread(
                publish_message, operations, request.state.principal, payload
            )
        except OperationError as err:
            return _operation_error(err)

        return PublishResult(message_id=outcome.message_id, warning=outcome.warning).to_dict()

    @app.get("/v1/query")
    async def query(request: Request) -> dict | tuple:
        try:
            qp = parse_query(request.query_params, max_limit=max_query_limit)
        except ContractError as err:
            return _bad_request(err)

        try:
            outcome = await asyncio.to_thread(
                query_messages, operations, request.state.principal, qp
            )
        except OperationError as err:
            return _operation_error(err)

        messages = outcome.messages
        return QueryResult(
            messages=messages,
            count=len(messages),
            total=outcome.total,
            offset=qp.offset,
            has_more=len(messages) == qp.limit,
        ).to_dict()
