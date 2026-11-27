"""Registry lookup route."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mansio_core.contracts import ContractError, RegistryLookupResult, parse_registry_lookup

from mansio_web._vendor.httpserver import App

from ._common import _bad_request

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register GET /v1/registry/lookup."""
    token_store = ctx.token_store

    @app.get("/v1/registry/lookup")
    async def registry_lookup(request: Request) -> dict | tuple:
        try:
            query = parse_registry_lookup(request.query_params)
        except ContractError as err:
            return _bad_request(err)
        user_id = query.user_id
        if token_store is not None and await asyncio.to_thread(token_store.user_exists, user_id):
            return RegistryLookupResult(found=True, user_id=user_id).to_dict()
        return RegistryLookupResult(found=False, user_id=user_id).to_dict()
