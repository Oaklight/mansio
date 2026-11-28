"""Operation-level tests for ``mansio_core.operations.admin`` (M6c-admin)."""

from __future__ import annotations

import pytest
from mansio_core.contracts import PublishRequest
from mansio_core.contracts.admin import AdminMessagesQuery
from mansio_core.operations import OperationContext, OperationError, admin
from mansio_core.operations import messages as message_operations
from mansio_core.principal import Principal
from mansio_core.types import Message, UserPresence


def _message(
    message_id: str = "m-1",
    *,
    channel: str = "general",
    sender: str = "alice",
    msg_type: str = "text",
    timestamp: str = "2026-01-01T00:00:00Z",
) -> Message:
    return Message(
        id=message_id,
        channel=channel,
        sender=sender,
        msg_type=msg_type,
        payload="hello",
        timestamp=timestamp,
    )


def _presence(user_id: str = "alice", status: str = "online") -> UserPresence:
    return UserPresence(
        user_id=user_id,
        status=status,
        last_seen="2026-01-01T00:00:00Z",
    )


class _FakeMetrics:
    def __init__(self) -> None:
        self.windows: list[int] = []

    def get_throughput(self, window_seconds: int) -> list:
        self.windows.append(window_seconds)
        return [{"window_seconds": window_seconds, "messages": 1}]


class _FakeBus:
    def __init__(self) -> None:
        self.stats_result = {"messages": 3}
        self.subscriptions_result = {"general": ["s1", "s2"], "quiet": []}
        self.channel_names = ["general", "quiet"]
        self.messages_by_channel = {
            "general": [
                _message("m-1", sender="alice", msg_type="text"),
                _message("m-2", sender="bob", msg_type="image"),
                _message("m-3", sender="alice", msg_type="text"),
            ],
            "quiet": [],
        }
        self.search_result = [_message("m-1")]
        self.info_result = {"type": "memory"}
        self.presence_result = [_presence("alice"), _presence("bob", "offline")]
        self.user_result: UserPresence | None = _presence("alice")
        self.users_error: Exception | None = None
        self.user_error: Exception | None = None
        self.metrics = _FakeMetrics()
        self.query_calls: list[tuple[str, int]] = []
        self.search_calls: list[dict] = []
        self.published: list[dict] = []

    def stats(self) -> dict:
        return self.stats_result

    def subscription_counts(self) -> dict[str, list[str]]:
        return self.subscriptions_result

    def channels(self) -> list[str]:
        return list(self.channel_names)

    def query(self, channel: str, limit: int = 100) -> list[Message]:
        self.query_calls.append((channel, limit))
        return self.messages_by_channel.get(channel, [])

    def search(self, **kwargs) -> list[Message]:
        self.search_calls.append(kwargs)
        return self.search_result

    def info(self) -> dict:
        return self.info_result

    def users(self, timeout_seconds: int = 120) -> list[UserPresence]:
        if self.users_error is not None:
            raise self.users_error
        return self.presence_result

    def user_status(self, user_id: str, timeout_seconds: int = 120) -> UserPresence | None:
        if self.user_error is not None:
            raise self.user_error
        return self.user_result

    def publish(self, **kwargs) -> str:
        self.published.append(kwargs)
        return "published-id"


class _FakeAuthz:
    def publish_denial(self, *args) -> None:
        raise AssertionError("admin publish must not consult channel ACLs")


@pytest.fixture()
def bus() -> _FakeBus:
    return _FakeBus()


@pytest.fixture()
def ctx(bus: _FakeBus) -> OperationContext:
    return OperationContext(
        bus=bus,
        authz=_FakeAuthz(),
        token_store=None,
        allow_unauthenticated=False,
    )


@pytest.fixture()
def no_auth_ctx(bus: _FakeBus) -> OperationContext:
    return OperationContext(
        bus=bus,
        authz=_FakeAuthz(),
        token_store=None,
        allow_unauthenticated=True,
    )


class TestRequireAdmin:
    def test_admin_is_allowed(self, ctx) -> None:
        admin.require_admin(ctx, Principal.admin())

    def test_no_auth_is_allowed_when_the_context_allows_it(self, no_auth_ctx) -> None:
        admin.require_admin(no_auth_ctx, Principal.no_auth())

    def test_no_auth_is_rejected_without_allow_unauthenticated(self, ctx) -> None:
        with pytest.raises(OperationError) as exc:
            admin.require_admin(ctx, Principal.no_auth())
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "Admin endpoints require an admin session"

    @pytest.mark.parametrize(
        "principal",
        [Principal.user("alice"), Principal.supertoken(), Principal.service()],
    )
    def test_other_principals_are_rejected(self, ctx, principal) -> None:
        with pytest.raises(OperationError) as exc:
            admin.require_admin(ctx, principal)
        assert exc.value.kind == "forbidden"


class TestReadOperations:
    def test_stats_adds_active_subscriptions(self, ctx, bus) -> None:
        assert admin.stats(ctx, Principal.admin()) == {
            "messages": 3,
            "active_subscriptions": 2,
        }

    def test_throughput_forwards_the_window(self, ctx, bus) -> None:
        assert admin.throughput(ctx, Principal.admin(), 30) == [
            {"window_seconds": 30, "messages": 1}
        ]
        assert bus.metrics.windows == [30]

    def test_channels_summarizes_each_channel(self, ctx, bus) -> None:
        summaries = admin.channels(ctx, Principal.admin())
        assert [summary.name for summary in summaries] == ["general", "quiet"]
        general, quiet = summaries
        assert general.message_count == 3
        assert general.sender_count == 2
        assert general.subscription_count == 2
        assert general.last_message_time == "2026-01-01T00:00:00Z"
        assert quiet.message_count == 0
        assert quiet.sender_count == 0
        assert quiet.subscription_count == 0
        assert quiet.last_message_time is None
        assert bus.query_calls == [("general", 1000), ("quiet", 1000)]

    def test_channel_detail_counts_senders_and_types(self, ctx) -> None:
        detail = admin.channel_detail(ctx, Principal.admin(), "general")
        assert detail is not None
        assert detail.message_count == 3
        assert detail.senders == ["alice", "bob"]
        assert detail.msg_types == [
            {"msg_type": "text", "count": 2},
            {"msg_type": "image", "count": 1},
        ]
        assert detail.last_message_time == "2026-01-01T00:00:00Z"

    def test_channel_detail_is_none_without_messages(self, ctx) -> None:
        assert admin.channel_detail(ctx, Principal.admin(), "quiet") is None

    def test_search_messages_forwards_the_query(self, ctx, bus) -> None:
        query = AdminMessagesQuery(channel="general", sender="alice", msg_type="text", limit=5)
        assert admin.search_messages(ctx, Principal.admin(), query) == bus.search_result
        assert bus.search_calls == [
            {"channel": "general", "sender": "alice", "msg_type": "text", "limit": 5}
        ]

    def test_subscriptions_are_returned_unchanged(self, ctx, bus) -> None:
        assert admin.subscriptions(ctx, Principal.admin()) is bus.subscriptions_result

    def test_backend_info_is_returned_unchanged(self, ctx, bus) -> None:
        assert admin.backend_info(ctx, Principal.admin()) is bus.info_result

    def test_presence_map_indexes_by_user(self, ctx) -> None:
        presence = admin.presence_map(ctx, Principal.admin())
        assert set(presence) == {"alice", "bob"}
        assert presence["bob"].status == "offline"

    def test_presence_map_is_empty_without_backend_presence(self, ctx, bus) -> None:
        bus.users_error = NotImplementedError
        assert admin.presence_map(ctx, Principal.admin()) == {}

    def test_user_status_returns_presence_or_none(self, ctx, bus) -> None:
        assert admin.user_status(ctx, Principal.admin(), "alice").user_id == "alice"
        bus.user_result = None
        assert admin.user_status(ctx, Principal.admin(), "missing") is None
        bus.user_error = NotImplementedError
        assert admin.user_status(ctx, Principal.admin(), "alice") is None


_READ_CALLS = [
    ("stats", lambda ctx: admin.stats(ctx, Principal.user("alice"))),
    ("throughput", lambda ctx: admin.throughput(ctx, Principal.user("alice"), 60)),
    ("channels", lambda ctx: admin.channels(ctx, Principal.user("alice"))),
    ("channel_detail", lambda ctx: admin.channel_detail(ctx, Principal.user("alice"), "general")),
    (
        "search_messages",
        lambda ctx: admin.search_messages(
            ctx, Principal.user("alice"), AdminMessagesQuery(channel="general")
        ),
    ),
    ("subscriptions", lambda ctx: admin.subscriptions(ctx, Principal.user("alice"))),
    ("backend_info", lambda ctx: admin.backend_info(ctx, Principal.user("alice"))),
    ("presence_map", lambda ctx: admin.presence_map(ctx, Principal.user("alice"))),
    ("user_status", lambda ctx: admin.user_status(ctx, Principal.user("alice"), "alice")),
]


class TestEveryOperationRequiresAdmin:
    @pytest.mark.parametrize(("name", "call"), _READ_CALLS, ids=[name for name, _ in _READ_CALLS])
    def test_scoped_user_is_rejected(self, ctx, name, call) -> None:
        with pytest.raises(OperationError) as exc:
            call(ctx)
        assert exc.value.kind == "forbidden", name


class TestAdminPublish:
    def test_admin_publish_skips_sender_and_acl_checks(self, ctx, bus) -> None:
        dto = PublishRequest(
            channel="general",
            sender="someone-else",
            msg_type="text",
            payload="hello",
        )
        outcome = message_operations.publish(ctx, Principal.admin(), dto)
        assert outcome.message_id == "published-id"
        assert bus.published[0]["sender"] == "someone-else"

    def test_admin_publish_still_validates_the_channel(self, ctx, bus) -> None:
        dto = PublishRequest(channel="x", sender="alice", msg_type="text", payload="hello")
        with pytest.raises(OperationError) as exc:
            message_operations.publish(ctx, Principal.admin(), dto)
        assert exc.value.kind == "bad_request"
        assert bus.published == []
