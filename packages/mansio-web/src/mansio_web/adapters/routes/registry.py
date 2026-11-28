"""Registry lookup route."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mansio_core.contracts import ContractError, RegistryLookupResult, parse_registry_lookup
from mansio_core.operations import OperationContext
from mansio_core.operations.channels import registry_lookup as registry_lookup_operation

from mansio_web._vendor.httpserver import App

from ._common import _bad_request

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register GET /v1/registry/lookup."""
    operations = OperationContext(
        bus=ctx.bus,
        authz=ctx.authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )

    @app.get("/v1/registry/lookup")
    async def registry_lookup(request: Request) -> dict | tuple:
        try:
            query = parse_registry_lookup(request.query_params)
        except ContractError as err:
            return _bad_request(err)

        found = await asyncio.to_thread(
            registry_lookup_operation, operations, request.state.principal, query
        )
        return RegistryLookupResult(found=found, user_id=query.user_id).to_dict()
