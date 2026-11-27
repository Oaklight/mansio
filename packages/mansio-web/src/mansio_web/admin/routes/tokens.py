"""Token CRUD routes."""

from __future__ import annotations

from typing import Any

from mansio_core.contracts import ContractError
from mansio_core.contracts.admin import parse_token_create

from mansio_web._vendor.httpserver import App

from . import RouteContext


def register(app: App, ctx: RouteContext) -> None:
    """Register token listing, creation, deletion and rotation."""
    token_store = ctx.token_store
    view = ctx.view

    @app.get("/api/tokens")
    def list_tokens(request: Any) -> dict:
        return view.tokens().to_dict()

    @app.post("/api/tokens")
    def create_token(request: Any) -> dict | tuple:
        if token_store is None:
            return {
                "error": "Service Unavailable",
                "message": "Token store not configured",
            }, 503
        try:
            payload = parse_token_create(request.json() if request.body else {})
        except ContractError as err:
            return err.as_error(), 400
        if payload.supertoken:
            entry = token_store.create_token(user_id=None, label=payload.label)
            return view.token_created(entry, supertoken=True).to_dict(), 201
        entry = token_store.create_token(user_id=payload.user_id, label=payload.label)
        return view.token_created(entry).to_dict(), 201

    @app.delete("/api/tokens/<token_id>")
    def delete_token(request: Any, token_id: str) -> dict | tuple:
        if token_store is None:
            return {
                "error": "Service Unavailable",
                "message": "Token store not configured",
            }, 503
        if token_store.delete_token(token_id):
            return view.token_deleted(token_id).to_dict()
        return {"error": "Not Found", "message": f"Token '{token_id}' not found"}, 404

    @app.post("/api/tokens/<token_id>/rotate")
    def rotate_token(request: Any, token_id: str) -> dict | tuple:
        if token_store is None:
            return {
                "error": "Service Unavailable",
                "message": "Token store not configured",
            }, 503
        result = token_store.rotate_token(token_id)
        if result is None:
            return {"error": "Not Found", "message": f"Token '{token_id}' not found"}, 404
        return view.token_rotated(result).to_dict()
