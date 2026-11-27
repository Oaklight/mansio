"""Message query and publish routes."""

from __future__ import annotations

from typing import Any

from mansio_core.contracts import ContractError, parse_publish
from mansio_core.contracts.admin import parse_admin_messages

from mansio_web._vendor.httpserver import App

from . import RouteContext, json_body


def register(app: App, ctx: RouteContext) -> None:
    """Register message reads and the publish route."""
    bus = ctx.bus

    @app.get("/api/messages")
    def get_messages(request: Any) -> dict | tuple:
        try:
            query = parse_admin_messages(request.query_params)
        except ContractError as err:
            return err.as_error(), 400
        view = ctx.view
        return view.messages(query).to_dict()

    @app.post("/api/messages")
    def publish_message(request: Any) -> dict | tuple:
        view = ctx.view
        try:
            payload = parse_publish(json_body(request))
        except ContractError as err:
            return err.as_error(), 400
        msg_id = bus.publish(
            channel=payload.channel,
            sender=payload.sender,
            msg_type=payload.msg_type,
            payload=payload.payload,
            metadata=payload.metadata,
        )
        return view.message_published(msg_id).to_dict()
