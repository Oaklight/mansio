"""Credential check route."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mansio_core.contracts import AuthCheckResult

from mansio_web._vendor.httpserver import App

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register GET /v1/auth/check."""
    token_store = ctx.token_store
    allow_unauthenticated = ctx.allow_unauthenticated

    @app.get("/v1/auth/check")
    async def auth_check(request: Request) -> dict:
        ts = token_store
        has_tokens = bool(ts and await asyncio.to_thread(ts.has_tokens))
        # Only the explicit opt-out disables auth. An adapter that merely
        # lacks a token store still requires a token it can never accept,
        # which is what the authenticated endpoints report too.
        return AuthCheckResult(
            auth_mode="disabled" if allow_unauthenticated else "required",
            has_tokens=has_tokens,
        ).to_dict()
