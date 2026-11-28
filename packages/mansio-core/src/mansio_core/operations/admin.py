"""Admin read use cases and the admin session gate.

Every operation requires an :class:`OperationContext` and a
:class:`Principal`, and calls :func:`require_admin` first.  Admin sessions
are the only wire identity for these routes; ``NO_AUTH`` is accepted only
when the composition root allows unauthenticated access.

The functions return store values or small frozen rows.  Response document
assembly stays in the web admin view; this module does not import
``mansio_web``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mansio_core.contracts.admin import AdminMessagesQuery
from mansio_core.operations import OperationContext, OperationError
from mansio_core.principal import Principal, PrincipalKind
from mansio_core.types import Message, UserPresence


@dataclass(frozen=True, slots=True)
class ChannelSummary:
    """One row of the admin channel list."""

    name: str
    message_count: int
    sender_count: int
    subscription_count: int
    last_message_time: str | None


@dataclass(frozen=True, slots=True)
class ChannelDetail:
    """Admin detail for one channel with at least one message."""

    name: str
    message_count: int
    senders: list[str]
    msg_types: list[dict]
    last_message_time: str


def require_admin(ctx: OperationContext, principal: Principal) -> None:
    """Raise ``forbidden`` unless *principal* may call an admin endpoint."""
    if principal.kind is PrincipalKind.ADMIN:
        return
    if principal.kind is PrincipalKind.NO_AUTH and ctx.allow_unauthenticated:
        return
    raise OperationError("forbidden", "Admin endpoints require an admin session")


def stats(ctx: OperationContext, principal: Principal) -> dict:
    """Return backend stats plus the number of active subscriptions."""
    require_admin(ctx, principal)
    result = dict(ctx.bus.stats())
    result["active_subscriptions"] = sum(len(ids) for ids in ctx.bus.subscription_counts().values())
    return result


def throughput(ctx: OperationContext, principal: Principal, window_seconds: int = 60) -> list:
    """Return the throughput buckets for *window_seconds*."""
    require_admin(ctx, principal)
    return ctx.bus.metrics.get_throughput(window_seconds)


def channels(ctx: OperationContext, principal: Principal) -> list[ChannelSummary]:
    """Summarize every channel from its latest 1000 messages."""
    require_admin(ctx, principal)
    subscriptions = ctx.bus.subscription_counts()
    summaries: list[ChannelSummary] = []
    for name in ctx.bus.channels():
        messages = ctx.bus.query(name, limit=1000)
        summaries.append(
            ChannelSummary(
                name=name,
                message_count=len(messages),
                sender_count=len({message.sender for message in messages}),
                subscription_count=len(subscriptions.get(name, [])),
                last_message_time=messages[-1].timestamp if messages else None,
            )
        )
    return summaries


def channel_detail(ctx: OperationContext, principal: Principal, name: str) -> ChannelDetail | None:
    """Return the admin detail for *name*, or ``None`` when it has no messages."""
    require_admin(ctx, principal)
    messages = ctx.bus.query(name, limit=1000)
    if not messages:
        return None
    type_counts: dict[str, int] = {}
    for message in messages:
        type_counts[message.msg_type] = type_counts.get(message.msg_type, 0) + 1
    return ChannelDetail(
        name=name,
        message_count=len(messages),
        senders=sorted({message.sender for message in messages}),
        msg_types=[
            {"msg_type": msg_type, "count": count} for msg_type, count in type_counts.items()
        ],
        last_message_time=messages[-1].timestamp,
    )


def search_messages(
    ctx: OperationContext, principal: Principal, query: AdminMessagesQuery
) -> list[Message]:
    """Return messages matching the admin filter."""
    require_admin(ctx, principal)
    return ctx.bus.search(
        channel=query.channel,
        sender=query.sender,
        msg_type=query.msg_type,
        limit=query.limit,
    )


def subscriptions(ctx: OperationContext, principal: Principal) -> dict[str, list[str]]:
    """Return subscription ids grouped by channel."""
    require_admin(ctx, principal)
    return ctx.bus.subscription_counts()


def backend_info(ctx: OperationContext, principal: Principal) -> dict:
    """Return backend information for the system screen."""
    require_admin(ctx, principal)
    return ctx.bus.info()


def presence_map(
    ctx: OperationContext, principal: Principal, timeout_seconds: int = 120
) -> dict[str, UserPresence]:
    """Return presence by user id; backends without presence report none."""
    require_admin(ctx, principal)
    try:
        users = ctx.bus.users(timeout_seconds=timeout_seconds)
    except NotImplementedError:
        return {}
    return {user.user_id: user for user in users}


def user_status(ctx: OperationContext, principal: Principal, user_id: str) -> UserPresence | None:
    """Return one user's presence, or ``None`` when the agent is unknown."""
    require_admin(ctx, principal)
    try:
        return ctx.bus.user_status(user_id)
    except NotImplementedError:
        return None
