"""Message use cases: publish, query, delete, cleanup, compact.

Each operation takes an :class:`OperationContext`, a required
:class:`Principal`, and a request DTO. It applies the channel and ACL
rules, calls the Bus, and returns a small outcome or raises
:class:`OperationError`. An adapter maps the outcome to the wire and the
error to a status; it performs no policy itself.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from mansio_core.authz import DM_NAME_EXPECTED, dm_participants
from mansio_core.authz import user_involved as _user_involved
from mansio_core.bus import Bus
from mansio_core.contracts import (
    AdminCleanupRequest,
    AdminCleanupResult,
    AdminCompactRequest,
    AdminCompactResult,
    PublishRequest,
    QueryRequest,
)
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.validation import invalid_dm_name, validate_channel_name
from mansio_core.principal import Principal
from mansio_core.types import Message


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """Result of :func:`publish`."""

    message_id: str
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """Result of :func:`query`; the adapter derives ``count`` and ``has_more``."""

    messages: list[Message]
    total: int


@dataclass(frozen=True, slots=True)
class DeleteMessageRequest:
    """Arguments of ``DELETE /v1/messages/<id>``."""

    message_id: str


@dataclass(frozen=True, slots=True)
class DeleteMessageOutcome:
    """Result of :func:`delete_message`."""

    message_id: str


def require_privileged(principal: Principal) -> None:
    """Raise ``forbidden`` unless *principal* is not bound to one user."""
    if not principal.is_privileged:
        raise OperationError("forbidden", "Admin endpoints require a supertoken")


def _dm_unregistered_warning(bus: Bus, channel: str, sender: str) -> str | None:
    """Return a warning string if a DM targets an unregistered agent."""
    participants = dm_participants(channel)
    if participants is None:
        return None
    other = participants[0] if participants[1] == sender else participants[1]
    try:
        status = bus.user_status(other)
    except NotImplementedError:
        return None
    if status is None:
        return f"target agent '{other}' is not registered; message stored but may never be read"
    return None


def _match_channels(channels: list[str], pattern: str) -> list[str]:
    """Match channel names against a glob-like pattern (``*`` is a wildcard)."""
    return [channel for channel in channels if fnmatch.fnmatch(channel, pattern)]


def publish(ctx: OperationContext, principal: Principal, dto: PublishRequest) -> PublishOutcome:
    """Validate the channel and sender, publish, and compute the DM warning."""
    validate_channel_name(dto.channel, token_store=ctx.token_store)

    user_id = principal.user_id
    if user_id is not None:
        if dto.sender != user_id:
            raise OperationError(
                "forbidden", f"Token bound to '{user_id}', cannot publish as '{dto.sender}'"
            )
        denial = ctx.authz.publish_denial(user_id, dto.channel, dto.msg_type)
        if denial:
            raise OperationError("forbidden", denial)

    try:
        message_id = ctx.bus.publish(
            channel=dto.channel,
            sender=dto.sender,
            msg_type=dto.msg_type,
            payload=dto.payload,
            metadata=dto.metadata,
            queue=dto.queue,
            parent_id=dto.parent_id,
            intent=dto.intent,
        )
    except ValueError as exc:
        message = str(exc)
        kind = "not_found" if "not found" in message else "bad_request"
        raise OperationError(kind, message) from exc

    return PublishOutcome(
        message_id=message_id,
        warning=_dm_unregistered_warning(ctx.bus, dto.channel, dto.sender),
    )


def query(ctx: OperationContext, principal: Principal, dto: QueryRequest) -> QueryOutcome:
    """Check read access, query the channel, and filter to visible messages."""
    if invalid_dm_name(dto.channel):
        raise OperationError("bad_request", DM_NAME_EXPECTED)

    user_id = principal.user_id
    if user_id is not None and not ctx.authz.can_read(user_id, dto.channel):
        raise OperationError(
            "forbidden", f"Token for '{user_id}' cannot query channel '{dto.channel}'"
        )

    messages = ctx.bus.query(
        dto.channel,
        after=dto.after,
        limit=dto.limit,
        msg_type=dto.msg_type,
        order=dto.order,
        thread_id=dto.thread_id,
        intent=dto.intent,
        offset=dto.offset,
    )
    if user_id is not None:
        messages = [m for m in messages if _user_involved(user_id, m.channel, m.sender)]

    return QueryOutcome(messages=messages, total=ctx.bus.message_count(dto.channel))


def delete_message(
    ctx: OperationContext, principal: Principal, dto: DeleteMessageRequest
) -> DeleteMessageOutcome:
    """Delete one message, enforcing the scoped-token ownership rule."""
    user_id = principal.user_id
    if user_id is not None:
        found = ctx.bus.get_message(dto.message_id)
        if found is None:
            raise OperationError("not_found", f"Message '{dto.message_id}' not found")
        if not ctx.authz.can_delete_message(user_id, found):
            raise OperationError(
                "forbidden",
                f"Token for '{user_id}' cannot delete messages from sender '{found.sender}'",
            )

    if not ctx.bus.delete_message(dto.message_id):
        raise OperationError("not_found", f"Message '{dto.message_id}' not found")
    return DeleteMessageOutcome(message_id=dto.message_id)


def cleanup(
    ctx: OperationContext, principal: Principal, dto: AdminCleanupRequest
) -> AdminCleanupResult:
    """Delete every non-system channel matching *dto.pattern*."""
    require_privileged(principal)

    matched = _match_channels(ctx.bus.channels(detail=False), dto.pattern)
    # Protect system channels from bulk deletion.
    matched = [channel for channel in matched if not channel.startswith("_system:")]

    if dto.older_than:
        try:
            detail_list = ctx.bus.channels_detail()
        except NotImplementedError:
            detail_list = []
        detail_map = {detail["name"]: detail for detail in detail_list}
        matched = [
            channel
            for channel in matched
            if channel in detail_map
            and detail_map[channel].get("last_activity", "") < dto.older_than
        ]

    total_deleted = 0
    channels_deleted: list[str] = []
    for channel in matched:
        total_deleted += ctx.bus.delete_channel(channel)
        channels_deleted.append(channel)

    return AdminCleanupResult(
        channels_deleted=len(channels_deleted),
        messages_deleted=total_deleted,
        channels=channels_deleted,
    )


def compact(
    ctx: OperationContext, principal: Principal, dto: AdminCompactRequest
) -> AdminCompactResult:
    """Compact one channel."""
    require_privileged(principal)
    removed = ctx.bus.compact(
        dto.channel,
        max_messages=dto.max_messages,
        keep_latest_per_sender=dto.keep_latest_per_sender,
    )
    return AdminCompactResult(channel=dto.channel, messages_removed=removed)
