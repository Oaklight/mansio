"""Channel prefix registration routes."""

from __future__ import annotations

from typing import Any

from mansio_core.contracts import ContractError
from mansio_core.contracts.admin import parse_prefix_create

from mansio_web._vendor.httpserver import App

from . import RouteContext, json_body


def register(app: App, ctx: RouteContext) -> None:
    """Register prefix listing, creation and deletion."""
    token_store = ctx.token_store
    view = ctx.view

    @app.get("/api/prefixes")
    def list_prefixes(request: Any) -> dict:
        return view.prefixes().to_dict()

    @app.post("/api/prefixes")
    def register_prefix(request: Any) -> dict | tuple:
        if token_store is None:
            return {
                "error": "Service Unavailable",
                "message": "Token store not configured",
            }, 503
        try:
            payload = parse_prefix_create(json_body(request) if request.body else {})
        except ContractError as err:
            return err.as_error(), 400
        result = token_store.register_prefix(prefix=payload.prefix, label=payload.label)
        if result is None:
            return {
                "error": "Bad Request",
                "message": f"'{payload.prefix}' is a built-in prefix and cannot be registered",
            }, 400
        return view.prefix_created(result).to_dict(), 201

    @app.delete("/api/prefixes/<prefix>")
    def delete_prefix(request: Any, prefix: str) -> dict | tuple:
        if token_store is None:
            return {
                "error": "Service Unavailable",
                "message": "Token store not configured",
            }, 503
        if token_store.delete_prefix(prefix):
            return view.prefix_deleted(prefix).to_dict()
        return {
            "error": "Not Found",
            "message": f"Prefix '{prefix}' not found or is built-in",
        }, 404
