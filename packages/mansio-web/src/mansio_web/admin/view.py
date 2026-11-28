"""Assemble admin API response documents from operations and the token store.

Routes call one method and serialize the returned document; this module is
the only place admin response bodies are built.  Bus reads and the admin
session gate live in :mod:`mansio_core.operations.admin`; this module maps
their values onto the admin contracts.
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
from mansio_core.operations import OperationContext
from mansio_core.operations import admin as admin_operations

if TYPE_CHECKING:
    from mansio_core.principal import Principal
    from mansio_core.token_store import TokenStore


class AdminView:
    """Read and mutation response builders for the admin API."""

    def __init__(self, operations: OperationContext, token_store: TokenStore | None) -> None:
        self._operations = operations
        self._token_store = token_store

    # ── Dashboard ────────────────────────────────────────────────

    def stats(self, principal: Principal) -> AdminStatsResult:
        return AdminStatsResult(stats=admin_operations.stats(self._operations, principal))

    def throughput(self, principal: Principal, window_seconds: int = 60) -> AdminThroughputResult:
        return AdminThroughputResult(
            window_seconds=window_seconds,
            buckets=admin_operations.throughput(self._operations, principal, window_seconds),
        )

    # ── Channels and messages ────────────────────────────────────

    def channels(self, principal: Principal) -> AdminChannelsResult:
        summaries = admin_operations.channels(self._operations, principal)
        return AdminChannelsResult(
            channels=[
                AdminChannelSummary(
                    name=summary.name,
                    message_count=summary.message_count,
                    sender_count=summary.sender_count,
                    subscription_count=summary.subscription_count,
                    last_message_time=summary.last_message_time,
                )
                for summary in summaries
            ]
        )

    def channel_detail(self, principal: Principal, name: str) -> AdminChannelDetail | None:
        detail = admin_operations.channel_detail(self._operations, principal, name)
        if detail is None:
            return None
        return AdminChannelDetail(
            name=detail.name,
            message_count=detail.message_count,
            senders=detail.senders,
            msg_types=detail.msg_types,
            last_message_time=detail.last_message_time,
        )

    def messages(self, principal: Principal, query: AdminMessagesQuery) -> AdminMessagesResult:
        messages = admin_operations.search_messages(self._operations, principal, query)
        return AdminMessagesResult(
            count=len(messages),
            messages=[_message_summary(message) for message in messages],
        )

    def message_published(self, message_id: str) -> AdminPublishResult:
        return AdminPublishResult(message_id=message_id)

    def subscriptions(self, principal: Principal) -> AdminSubscriptionsResult:
        counts = admin_operations.subscriptions(self._operations, principal)
        channels = [
            {"channel": channel, "subscription_ids": ids, "count": len(ids)}
            for channel, ids in counts.items()
        ]
        return AdminSubscriptionsResult(
            total=sum(len(ids) for ids in counts.values()), channels=channels
        )

    def system(self, principal: Principal) -> AdminSystemResult:
        import mansio_web as mansio

        configured = 0
        if self._token_store is not None:
            configured = len(self._token_store.list_tokens())
        return AdminSystemResult(
            version=mansio.__version__,
            backend=admin_operations.backend_info(self._operations, principal),
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

    def users(self, principal: Principal) -> AdminUsersResult:
        if self._token_store is None:
            return AdminUsersResult(users=[], enabled=False)
        users = self._token_store.list_users()
        presence = admin_operations.presence_map(self._operations, principal)
        for user in users:
            entry = presence.get(user["user_id"])
            user["status"] = entry.status if entry else "unknown"
            user["last_seen"] = entry.last_seen if entry else None
        return AdminUsersResult(users=users, enabled=True)

    def user_registered(self, user_id: str, entry: dict) -> AdminUserRegisteredResult:
        return AdminUserRegisteredResult(user_id=user_id, token=entry)

    def user_detail(self, principal: Principal, user_id: str) -> AdminUserDetailResult | None:
        assert self._token_store is not None
        tokens = self._token_store.list_user_tokens(user_id)
        if not tokens:
            return None
        presence = admin_operations.user_status(self._operations, principal, user_id)
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
