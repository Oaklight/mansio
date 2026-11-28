"""Stream subscription use cases.

The subscribe-time checks, the per-message visibility filter, and the
replay query live here so every streaming adapter applies the same rule.
Event formatting and backpressure stay in the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mansio_core.authz import DM_NAME_EXPECTED
from mansio_core.authz import user_involved as _user_involved
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.validation import invalid_dm_name
from mansio_core.principal import Principal

if TYPE_CHECKING:
    from collections.abc import Callable

    from mansio_core.types import Message


@dataclass(frozen=True, slots=True)
class StreamRequest:
    """Channels and filters of one subscription."""

    channels: list[str]
    exclude_own: bool = False


def validate(ctx: OperationContext, principal: Principal, dto: StreamRequest) -> None:
    """Raise the subscribe-time errors: a bad DM name, then a read denial."""
    for channel in dto.channels:
        if invalid_dm_name(channel):
            raise OperationError("bad_request", f"{DM_NAME_EXPECTED} (got '{channel}')")

    user_id = principal.user_id
    if user_id is not None:
        forbidden = [
            channel for channel in dto.channels if not ctx.authz.can_read(user_id, channel)
        ]
        if forbidden:
            raise OperationError(
                "forbidden",
                f"Token for '{user_id}' cannot subscribe to: {', '.join(forbidden)}",
            )


def allows(principal: Principal, channel: str, sender: str, exclude_own: bool) -> bool:
    """Whether one message belongs on the subscription."""
    user_id = principal.user_id
    if user_id is not None and not _user_involved(user_id, channel, sender):
        return False
    return not (exclude_own and sender == user_id)


def subscribe(
    ctx: OperationContext,
    principal: Principal,
    dto: StreamRequest,
    on_message: Callable[[str, Message], None],
) -> list[str]:
    """Validate the request and subscribe every channel.

    *on_message* receives the target channel and the message after the
    visibility filter, from the publisher's thread.
    """
    validate(ctx, principal, dto)

    sub_ids: list[str] = []
    for channel in dto.channels:

        def _callback(message: Message, target: str = channel) -> None:
            if allows(principal, message.channel, message.sender, dto.exclude_own):
                on_message(target, message)

        sub_ids.append(ctx.bus.subscribe(channel, _callback))
    return sub_ids


def unsubscribe(ctx: OperationContext, sub_ids: list[str]) -> None:
    """Drop every subscription in *sub_ids*."""
    for sub_id in sub_ids:
        ctx.bus.unsubscribe(sub_id)


def replay(
    ctx: OperationContext,
    principal: Principal,
    dto: StreamRequest,
    last_event_id: str,
    limit: int,
) -> list[tuple[str, list[Message], bool]]:
    """Return ``(channel, visible messages, truncated)`` per channel.

    *truncated* is true when the raw query filled *limit*, whether or not
    more rows exist; the caller emits the warning before the channel's
    events, as before.
    """
    result: list[tuple[str, list[Message], bool]] = []
    for channel in dto.channels:
        missed = ctx.bus.query(channel, after=last_event_id, limit=limit)
        truncated = len(missed) >= limit
        visible = [m for m in missed if allows(principal, m.channel, m.sender, dto.exclude_own)]
        result.append((channel, visible, truncated))
    return result
