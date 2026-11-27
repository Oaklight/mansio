"""Assemble admin API response documents from the bus and token store.

Routes call one method and serialize the returned document; this module is
the only place admin response bodies are built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mansio_core.contracts.admin import (
    AdminChannelDetail,
    AdminChannelsResult,
    AdminChannelSummary,
    AdminMessagesQuery,
    AdminMessagesResult,
    AdminPrefixCreatedResult,
    AdminPrefixDeletedResult,
    AdminPrefixesResult,
    AdminPublishResult,
    AdminStatsResult,
    AdminSubscriptionsResult,
    AdminSystemResult,
    AdminThroughputResult,
    AdminTokenCreatedResult,
    AdminTokenDeletedResult,
    AdminTokenRotatedResult,
    AdminTokensResult,
    AdminUserDeletedResult,
    AdminUserDetailResult,
    AdminUserRegisteredResult,
    AdminUsersResult,
    AdminUserTokensResult,
)

if TYPE_CHECKING:
    from mansio_core.bus import Bus
    from mansio_core.token_store import TokenStore


class AdminView:
    """Read and mutation response builders for the admin API."""

    def __init__(self, bus: Bus, token_store: TokenStore | None) -> None:
        self._bus = bus
        self._token_store = token_store

    # ── Dashboard ────────────────────────────────────────────────

    def stats(self) -> AdminStatsResult:
        stats = dict(self._bus.stats())
        stats["active_subscriptions"] = sum(
            len(ids) for ids in self._bus.subscription_counts().values()
        )
        return AdminStatsResult(stats=stats)

    def throughput(self, window_seconds: int = 60) -> AdminThroughputResult:
        return AdminThroughputResult(
            window_seconds=window_seconds,
            buckets=self._bus.metrics.get_throughput(window_seconds),
        )

    # ── Channels and messages ────────────────────────────────────

    def channels(self) -> AdminChannelsResult:
        subscriptions = self._bus.subscription_counts()
        summaries = []
        for name in self._bus.channels():
            messages = self._bus.query(name, limit=1000)
            summaries.append(
                AdminChannelSummary(
                    name=name,
                    message_count=len(messages),
                    sender_count=len({m.sender for m in messages}),
                    subscription_count=len(subscriptions.get(name, [])),
                    last_message_time=messages[-1].timestamp if messages else None,
                )
            )
        return AdminChannelsResult(channels=summaries)

    def channel_detail(self, name: str) -> AdminChannelDetail | None:
        messages = self._bus.query(name, limit=1000)
        if not messages:
            return None
        type_counts: dict[str, int] = {}
        for message in messages:
            type_counts[message.msg_type] = type_counts.get(message.msg_type, 0) + 1
        return AdminChannelDetail(
            name=name,
            message_count=len(messages),
            senders=sorted({m.sender for m in messages}),
            msg_types=[
                {"msg_type": msg_type, "count": count} for msg_type, count in type_counts.items()
            ],
            last_message_time=messages[-1].timestamp,
        )

    def messages(self, query: AdminMessagesQuery) -> AdminMessagesResult:
        messages = self._bus.search(
            channel=query.channel,
            sender=query.sender,
            msg_type=query.msg_type,
            limit=query.limit,
        )
        return AdminMessagesResult(
            count=len(messages),
            messages=[_message_summary(message) for message in messages],
        )

    def message_published(self, message_id: str) -> AdminPublishResult:
        return AdminPublishResult(message_id=message_id)

    def subscriptions(self) -> AdminSubscriptionsResult:
        counts = self._bus.subscription_counts()
        channels = [
            {"channel": channel, "subscription_ids": ids, "count": len(ids)}
            for channel, ids in counts.items()
        ]
        return AdminSubscriptionsResult(
            total=sum(len(ids) for ids in counts.values()), channels=channels
        )

    def system(self) -> AdminSystemResult:
        import mansio_web as mansio

        configured = 0
        if self._token_store is not None:
            configured = len(self._token_store.list_tokens())
        return AdminSystemResult(
            version=mansio.__version__,
            backend=self._bus.info(),
            tokens={"configured": configured},
        )

    # ── Prefixes ─────────────────────────────────────────────────

    def prefixes(self) -> AdminPrefixesResult:
        if self._token_store is None:
            return AdminPrefixesResult(prefixes=[], enabled=False)
        return AdminPrefixesResult(prefixes=self._token_store.list_prefixes(), enabled=True)

    def prefix_created(self, prefix: dict) -> AdminPrefixCreatedResult:
        return AdminPrefixCreatedResult(prefix=prefix)

    def prefix_deleted(self, prefix: str) -> AdminPrefixDeletedResult:
        return AdminPrefixDeletedResult(deleted=prefix)

    # ── Users ────────────────────────────────────────────────────

    def users(self) -> AdminUsersResult:
        if self._token_store is None:
            return AdminUsersResult(users=[], enabled=False)
        users = self._token_store.list_users()
        try:
            presence = {a.user_id: a for a in self._bus.users(timeout_seconds=120)}
        except NotImplementedError:
            presence = {}
        for user in users:
            entry = presence.get(user["user_id"])
            user["status"] = entry.status if entry else "unknown"
            user["last_seen"] = entry.last_seen if entry else None
        return AdminUsersResult(users=users, enabled=True)

    def user_registered(self, user_id: str, entry: dict) -> AdminUserRegisteredResult:
        return AdminUserRegisteredResult(user_id=user_id, token=entry)

    def user_detail(self, user_id: str) -> AdminUserDetailResult | None:
        assert self._token_store is not None
        tokens = self._token_store.list_user_tokens(user_id)
        if not tokens:
            return None
        try:
            presence = self._bus.user_status(user_id)
        except NotImplementedError:
            presence = None
        return AdminUserDetailResult(
            user_id=user_id,
            tokens=tokens,
            status=presence.status if presence else "unknown",
            last_seen=presence.last_seen if presence else None,
        )

    def user_deleted(self, count: int) -> AdminUserDeletedResult:
        return AdminUserDeletedResult(deleted_tokens=count)

    def user_tokens(self, user_id: str) -> AdminUserTokensResult:
        assert self._token_store is not None
        return AdminUserTokensResult(
            user_id=user_id, tokens=self._token_store.list_user_tokens(user_id)
        )

    def user_token_created(self, entry: dict) -> AdminTokenCreatedResult:
        return AdminTokenCreatedResult(token=entry)

    # ── Tokens ───────────────────────────────────────────────────

    def tokens(self) -> AdminTokensResult:
        if self._token_store is None:
            return AdminTokensResult(tokens=[], enabled=False)
        return AdminTokensResult(tokens=self._token_store.list_tokens(), enabled=True)

    def token_created(
        self, entry: dict, *, supertoken: bool | None = None
    ) -> AdminTokenCreatedResult:
        return AdminTokenCreatedResult(supertoken=supertoken, token=entry)

    def token_deleted(self, token_id: str) -> AdminTokenDeletedResult:
        return AdminTokenDeletedResult(deleted=token_id)

    def token_rotated(self, entry: dict) -> AdminTokenRotatedResult:
        return AdminTokenRotatedResult(token=entry)


_ADMIN_MESSAGE_FIELDS = (
    "id",
    "channel",
    "sender",
    "msg_type",
    "payload",
    "timestamp",
    "metadata",
)


def _message_summary(message: Any) -> dict:
    """Serialize a message with the admin projection.

    The admin API keeps its original seven keys.  ``message_to_dict`` (the
    REST/SSE serializer) adds ``parent_id``, ``thread_id`` and ``intent``
    when set, which the admin panel never emitted.
    """
    return {name: getattr(message, name) for name in _ADMIN_MESSAGE_FIELDS}
