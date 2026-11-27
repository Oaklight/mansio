"""Message and channel deletion routes, including admin maintenance."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from mansio_core.contracts import (
    AdminCleanupResult,
    AdminCompactResult,
    ContractError,
    DeleteChannelResult,
    DeleteMessageResult,
    parse_admin_cleanup,
    parse_admin_compact,
)

from mansio_web._vendor.httpserver import App

from ._common import _bad_request, _error, _json_body

if TYPE_CHECKING:
    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


def _match_channels(channels: list[str], pattern: str) -> list[str]:
    """Match channel names against a glob-like pattern.

    Supports ``*`` as a wildcard for any sequence of characters.
    The pattern is matched against the full channel name.

    Args:
        channels: List of channel names to match.
        pattern: Glob pattern (e.g. ``"test:*"``, ``"*"``)

    Returns:
        List of matching channel names.
    """
    import fnmatch

    return [ch for ch in channels if fnmatch.fnmatch(ch, pattern)]


def register(app: App, ctx: RouteContext) -> None:
    """Register deletion and admin routes."""
    _register_delete_routes(app, ctx)
    _register_admin_routes(app, ctx)


def _register_delete_routes(app: App, ctx: RouteContext) -> None:
    bus = ctx.bus
    authz = ctx.authz

    @app.delete("/v1/channels/<channel_name>")
    async def delete_channel(request: Request, channel_name: str = "") -> dict | tuple:
        if not channel_name:
            return _error("Bad Request", "Channel name required", 400)

        auth_result = request.state.auth_result
        if isinstance(auth_result, str):
            denial = authz.delete_channel_denial(auth_result, channel_name)
            if denial:
                return _error("Forbidden", denial, 403)

        try:
            count = await asyncio.to_thread(bus.delete_channel, channel_name)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support deletion", 501)

        if count == 0:
            return _error("Not Found", f"Channel '{channel_name}' not found or already empty", 404)

        return DeleteChannelResult(deleted=count, channel=channel_name).to_dict()

    @app.delete("/v1/messages/<message_id>")
    async def delete_message(request: Request, message_id: str = "") -> dict | tuple:
        if not message_id:
            return _error("Bad Request", "Message ID required", 400)

        auth_result = request.state.auth_result

        # For scoped tokens, verify the message belongs to the agent
        if isinstance(auth_result, str):
            found_msg = await asyncio.to_thread(bus.get_message, message_id)

            if found_msg is None:
                return _error("Not Found", f"Message '{message_id}' not found", 404)

            if not authz.can_delete_message(auth_result, found_msg):
                return _error(
                    "Forbidden",
                    f"Token for '{auth_result}' cannot delete messages "
                    f"from sender '{found_msg.sender}'",
                    403,
                )

        try:
            deleted = await asyncio.to_thread(bus.delete_message, message_id)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support deletion", 501)

        if not deleted:
            return _error("Not Found", f"Message '{message_id}' not found", 404)

        return DeleteMessageResult(deleted=True, message_id=message_id).to_dict()


def _register_admin_routes(app: App, ctx: RouteContext) -> None:
    _register_admin_cleanup(app, ctx)
    _register_admin_compact(app, ctx)


def _register_admin_cleanup(app: App, ctx: RouteContext) -> None:
    bus = ctx.bus

    @app.post("/v1/admin/channels/cleanup")
    async def admin_cleanup(request: Request) -> dict | tuple:
        auth_result = request.state.auth_result

        # Admin only: supertoken (None) or no-auth mode (True)
        if isinstance(auth_result, str):
            return _error("Forbidden", "Admin endpoints require a supertoken", 403)

        try:
            cleanup = parse_admin_cleanup(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        pattern = cleanup.pattern
        older_than = cleanup.older_than

        all_channels = cast(list[str], await asyncio.to_thread(bus.channels, detail=False))
        matched = _match_channels(all_channels, pattern)

        # Protect system channels from bulk deletion
        matched = [ch for ch in matched if not ch.startswith("_system:")]

        if older_than:
            # Filter by last activity time
            try:
                detail_list = await asyncio.to_thread(bus.channels_detail)
            except NotImplementedError:
                detail_list = []
            detail_map = {d["name"]: d for d in detail_list}
            matched = [
                ch
                for ch in matched
                if ch in detail_map and detail_map[ch].get("last_activity", "") < older_than
            ]

        total_deleted = 0
        channels_deleted: list[str] = []

        try:
            for ch in matched:
                count = await asyncio.to_thread(bus.delete_channel, ch)
                total_deleted += count
                channels_deleted.append(ch)
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support deletion", 501)

        return AdminCleanupResult(
            channels_deleted=len(channels_deleted),
            messages_deleted=total_deleted,
            channels=channels_deleted,
        ).to_dict()


def _register_admin_compact(app: App, ctx: RouteContext) -> None:
    bus = ctx.bus

    @app.post("/v1/admin/compact")
    async def admin_compact(request: Request) -> dict | tuple:
        auth_result = request.state.auth_result

        # Admin only: supertoken (None) or no-auth mode (True)
        if isinstance(auth_result, str):
            return _error("Forbidden", "Admin endpoints require a supertoken", 403)

        try:
            compact = parse_admin_compact(_json_body(request))
        except ContractError as err:
            return _bad_request(err)

        channel = compact.channel
        max_messages = compact.max_messages
        keep_latest_per_sender = compact.keep_latest_per_sender

        try:
            removed = await asyncio.to_thread(
                bus.compact,
                channel,
                max_messages=max_messages,
                keep_latest_per_sender=keep_latest_per_sender,
            )
        except NotImplementedError:
            return _error("Not Implemented", "Backend does not support compaction", 501)

        return AdminCompactResult(channel=channel, messages_removed=removed).to_dict()
