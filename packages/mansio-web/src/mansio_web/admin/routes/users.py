"""User CRUD and per-user token routes."""

from __future__ import annotations

from typing import Any

from mansio_core.contracts import ContractError
from mansio_core.contracts.admin import parse_user_create, parse_user_token_create

from mansio_web._vendor.httpserver import App

from . import RouteContext, json_body


def register(app: App, ctx: RouteContext) -> None:
    """Register user listing, registration, detail, deletion and tokens."""
    _register_user_routes(app, ctx)
    _register_user_token_routes(app, ctx)


def _register_user_routes(app: App, ctx: RouteContext) -> None:
    token_store = ctx.token_store
    view = ctx.view

    @app.get("/api/users")
    def list_users(request: Any) -> dict:
        return view.users().to_dict()

    @app.post("/api/users")
    def register_user(request: Any) -> dict | tuple:
        if token_store is None:
            return {
                "error": "Service Unavailable",
                "message": "Token store not configured",
            }, 503
        try:
            payload = parse_user_create(json_body(request) if request.body else {})
        except ContractError as err:
            return err.as_error(), 400
        if token_store.user_exists(payload.user_id):
            return {
                "error": "Conflict",
                "message": f"User '{payload.user_id}' already exists",
            }, 409
        entry = token_store.create_token(user_id=payload.user_id, label=payload.label)
        return view.user_registered(payload.user_id, entry).to_dict(), 201

    @app.get("/api/users/<user_id>")
    def get_user(request: Any, user_id: str) -> dict | tuple:
        if token_store is None:
            return {"error": "Service Unavailable"}, 503
        detail = view.user_detail(user_id)
        if detail is None:
            return {"error": "Not Found", "message": f"User '{user_id}' not found"}, 404
        return detail.to_dict()

    @app.delete("/api/users/<user_id>")
    def delete_user(request: Any, user_id: str) -> dict | tuple:
        if token_store is None:
            return {"error": "Service Unavailable"}, 503
        count = token_store.delete_user(user_id)
        if count == 0:
            return {"error": "Not Found", "message": f"User '{user_id}' not found"}, 404
        return view.user_deleted(count).to_dict()


def _register_user_token_routes(app: App, ctx: RouteContext) -> None:
    token_store = ctx.token_store
    view = ctx.view

    @app.get("/api/users/<user_id>/tokens")
    def list_user_tokens(request: Any, user_id: str) -> dict | tuple:
        if token_store is None:
            return {"error": "Service Unavailable"}, 503
        return view.user_tokens(user_id).to_dict()

    @app.post("/api/users/<user_id>/tokens")
    def create_user_token(request: Any, user_id: str) -> dict | tuple:
        if token_store is None:
            return {"error": "Service Unavailable"}, 503
        if not token_store.user_exists(user_id):
            return {"error": "Not Found", "message": f"User '{user_id}' not found"}, 404
        try:
            label = parse_user_token_create(json_body(request) if request.body else {})
        except ContractError as err:
            return err.as_error(), 400
        entry = token_store.create_token(user_id=user_id, label=label)
        return view.user_token_created(entry).to_dict(), 201
