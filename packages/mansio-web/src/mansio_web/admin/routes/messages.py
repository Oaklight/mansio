"""Message query and publish routes."""

from __future__ import annotations

from typing import Any

from mansio_core.contracts import ContractError, parse_publish
from mansio_core.contracts.admin import parse_admin_messages
from mansio_core.operations import OperationError
from mansio_core.operations import messages as message_operations

from mansio_web._vendor.httpserver import App

from . import RouteContext, json_body, operation_error


def register(app: App, ctx: RouteContext) -> None:
    """Register message reads and the publish route."""
    view = ctx.view

    @app.get("/api/messages")
    def get_messages(request: Any) -> dict | tuple:
        try:
            query = parse_admin_messages(request.query_params)
        except ContractError as err:
            return err.as_error(), 400
        try:
            return view.messages(request.state.principal, query).to_dict()
        except OperationError as err:
            return operation_error(err)

    @app.post("/api/messages")
    def publish_message(request: Any) -> dict | tuple:
        try:
            payload = parse_publish(json_body(request))
        except ContractError as err:
            return err.as_error(), 400
        try:
            outcome = message_operations.publish(ctx.operations, request.state.principal, payload)
        except OperationError as err:
            return operation_error(err)
        return view.message_published(outcome.message_id).to_dict()
