"""Operation-level tests for ``mansio_core.operations.messages`` (M6b)."""

from __future__ import annotations

import pytest
from mansio_core.contracts import (
    AdminCleanupRequest,
    AdminCompactRequest,
    PublishRequest,
    QueryRequest,
)
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.messages import (
    DeleteMessageRequest,
    cleanup,
    compact,
    delete_message,
    publish,
    query,
    require_privileged,
)
from mansio_core.principal import Principal
from mansio_core.types import Message


def _message(message_id: str, channel: str, sender: str) -> Message:
    return Message(
        id=message_id,
        channel=channel,
        sender=sender,
        msg_type="chat",
        payload="hi",
        timestamp="2026-01-01T00:00:00Z",
    )


class _FakeAuthz:
    def __init__(self) -> None:
        self.publish_denial_result: str | None = None
        self.can_read_result = True
        self.can_delete_result = True
        self.calls: list[tuple] = []

    def publish_denial(self, user_id: str, channel: str, msg_type: str = "") -> str | None:
        self.calls.append(("publish_denial", user_id, channel))
        return self.publish_denial_result

    def can_read(self, user_id: str, channel: str) -> bool:
        self.calls.append(("can_read", user_id, channel))
        return self.can_read_result

    def can_delete_message(self, user_id: str, message: Message | None) -> bool:
        self.calls.append(("can_delete_message", user_id))
        return self.can_delete_result


class _FakeBus:
    def __init__(self) -> None:
        self.published: dict | None = None
        self.publish_result = "msg-1"
        self.publish_error: Exception | None = None
        self.user_status_result: object | None = None
        self.user_status_error: Exception | None = None
        self.query_result: list[Message] = []
        self.query_calls: list[tuple] = []
        self.total = 0
        self.get_message_result: Message | None = None
        self.delete_message_result = True
        self.deleted_message_ids: list[str] = []
        self.channels_result: list[str] = []
        self.detail_result: list[dict] = []
        self.deleted_channels: list[str] = []
        self.delete_channel_error: Exception | None = None
        self.compact_result = 0
        self.compact_error: Exception | None = None

    def publish(self, **kwargs) -> str:
        if self.publish_error is not None:
            raise self.publish_error
        self.published = kwargs
        return self.publish_result

    def user_status(self, user_id: str) -> object | None:
        if self.user_status_error is not None:
            raise self.user_status_error
        return self.user_status_result

    def query(self, channel: str, **kwargs) -> list[Message]:
        self.query_calls.append((channel, kwargs))
        return self.query_result

    def message_count(self, channel: str) -> int:
        return self.total

    def get_message(self, message_id: str) -> Message | None:
        return self.get_message_result

    def delete_message(self, message_id: str) -> bool:
        self.deleted_message_ids.append(message_id)
        return self.delete_message_result

    def channels(self, detail: bool = False) -> list[str]:
        return self.channels_result

    def channels_detail(self) -> list[dict]:
        return self.detail_result

    def delete_channel(self, channel: str) -> int:
        if self.delete_channel_error is not None:
            raise self.delete_channel_error
        self.deleted_channels.append(channel)
        return 1

    def compact(self, channel: str, **kwargs) -> int:
        if self.compact_error is not None:
            raise self.compact_error
        return self.compact_result


@pytest.fixture()
def bus() -> _FakeBus:
    return _FakeBus()


@pytest.fixture()
def authz() -> _FakeAuthz:
    return _FakeAuthz()


@pytest.fixture()
def ctx(bus: _FakeBus, authz: _FakeAuthz) -> OperationContext:
    return OperationContext(
        bus=bus,
        authz=authz,
        token_store=None,
        allow_unauthenticated=False,
    )


def _publish_dto(**overrides) -> PublishRequest:
    values = {
        "channel": "agents",
        "sender": "agent-1",
        "msg_type": "chat",
        "payload": "hello",
    }
    values.update(overrides)
    return PublishRequest(**values)


class TestPublish:
    def test_rejects_an_invalid_channel(self, ctx, bus) -> None:
        with pytest.raises(OperationError) as exc:
            publish(ctx, Principal.user("agent-1"), _publish_dto(channel="BAD"))
        assert exc.value.kind == "bad_request"
        assert "Channel name must be" in exc.value.message
        assert bus.published is None

    def test_rejects_a_sender_the_token_does_not_own(self, ctx, bus) -> None:
        with pytest.raises(OperationError) as exc:
            publish(ctx, Principal.user("agent-1"), _publish_dto(sender="agent-2"))
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "Token bound to 'agent-1', cannot publish as 'agent-2'"
        assert bus.published is None

    def test_rejects_a_publish_the_acl_denies(self, ctx, bus, authz) -> None:
        authz.publish_denial_result = "read-only channel"
        with pytest.raises(OperationError) as exc:
            publish(ctx, Principal.user("agent-1"), _publish_dto())
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "read-only channel"
        assert bus.published is None

    def test_maps_a_missing_channel_to_not_found(self, ctx, bus) -> None:
        bus.publish_error = ValueError("channel 'agents' not found")
        with pytest.raises(OperationError) as exc:
            publish(ctx, Principal.user("agent-1"), _publish_dto())
        assert exc.value.kind == "not_found"
        assert exc.value.message == "channel 'agents' not found"

    def test_maps_any_other_value_error_to_bad_request(self, ctx, bus) -> None:
        bus.publish_error = ValueError("metadata must be an object")
        with pytest.raises(OperationError) as exc:
            publish(ctx, Principal.user("agent-1"), _publish_dto())
        assert exc.value.kind == "bad_request"

    def test_privileged_publish_skips_sender_and_acl(self, ctx, bus, authz) -> None:
        outcome = publish(ctx, Principal.supertoken(), _publish_dto(sender="anyone"))
        assert outcome.message_id == "msg-1"
        assert authz.calls == []
        assert bus.published is not None
        assert bus.published["sender"] == "anyone"

    def test_dm_to_an_unregistered_agent_carries_the_warning(self, ctx, bus) -> None:
        bus.user_status_result = None
        outcome = publish(
            ctx,
            Principal.user("agent-1"),
            _publish_dto(channel="dm:agent-1:agent-9"),
        )
        assert outcome.warning == (
            "target agent 'agent-9' is not registered; message stored but may never be read"
        )

    def test_dm_to_a_registered_agent_has_no_warning(self, ctx, bus) -> None:
        bus.user_status_result = {"user_id": "agent-9"}
        outcome = publish(
            ctx,
            Principal.user("agent-1"),
            _publish_dto(channel="dm:agent-1:agent-9"),
        )
        assert outcome.warning is None

    def test_dm_ignores_a_backend_without_presence(self, ctx, bus) -> None:
        bus.user_status_error = NotImplementedError
        outcome = publish(
            ctx,
            Principal.user("agent-1"),
            _publish_dto(channel="dm:agent-1:agent-9"),
        )
        assert outcome.warning is None

    def test_a_plain_channel_never_warns(self, ctx, bus) -> None:
        outcome = publish(ctx, Principal.user("agent-1"), _publish_dto())
        assert outcome.warning is None


class TestQuery:
    def test_denies_a_channel_the_token_cannot_read(self, ctx, bus, authz) -> None:
        authz.can_read_result = False
        with pytest.raises(OperationError) as exc:
            query(ctx, Principal.user("agent-1"), QueryRequest(channel="private-room"))
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "Token for 'agent-1' cannot query channel 'private-room'"
        assert bus.query_calls == []

    def test_filters_a_scoped_caller_to_involved_messages(self, ctx, bus) -> None:
        visible = _message("m1", "notebook:agent-2", "agent-1")
        hidden = _message("m2", "notebook:agent-2", "agent-3")
        bus.query_result = [visible, hidden]
        bus.total = 2

        outcome = query(ctx, Principal.user("agent-1"), QueryRequest(channel="notebook:agent-2"))

        assert [m.id for m in outcome.messages] == ["m1"]
        assert outcome.total == 2

    def test_privileged_query_sees_everything(self, ctx, bus, authz) -> None:
        bus.query_result = [
            _message("m1", "dm:agent-1:agent-2", "agent-1"),
            _message("m2", "dm:agent-1:agent-2", "agent-3"),
        ]
        outcome = query(ctx, Principal.supertoken(), QueryRequest(channel="agents"))
        assert len(outcome.messages) == 2
        assert authz.calls == []

    def test_rejects_a_malformed_dm_name(self, ctx) -> None:
        with pytest.raises(OperationError) as exc:
            query(ctx, Principal.user("agent-1"), QueryRequest(channel="dm:onlyone"))
        assert exc.value.kind == "bad_request"


class TestDeleteMessage:
    def test_reports_a_missing_message(self, ctx, bus) -> None:
        with pytest.raises(OperationError) as exc:
            delete_message(ctx, Principal.user("agent-1"), DeleteMessageRequest("m1"))
        assert exc.value.kind == "not_found"
        assert exc.value.message == "Message 'm1' not found"

    def test_denies_a_message_the_token_does_not_own(self, ctx, bus, authz) -> None:
        bus.get_message_result = _message("m1", "agents", "agent-2")
        authz.can_delete_result = False
        with pytest.raises(OperationError) as exc:
            delete_message(ctx, Principal.user("agent-1"), DeleteMessageRequest("m1"))
        assert exc.value.kind == "forbidden"
        assert exc.value.message == (
            "Token for 'agent-1' cannot delete messages from sender 'agent-2'"
        )
        assert bus.deleted_message_ids == []

    def test_deletes_the_message(self, ctx, bus, authz) -> None:
        bus.get_message_result = _message("m1", "agents", "agent-1")
        outcome = delete_message(ctx, Principal.user("agent-1"), DeleteMessageRequest("m1"))
        assert outcome.message_id == "m1"
        assert bus.deleted_message_ids == ["m1"]

    def test_privileged_delete_skips_the_lookup(self, ctx, bus) -> None:
        delete_message(ctx, Principal.supertoken(), DeleteMessageRequest("m1"))
        assert bus.deleted_message_ids == ["m1"]

    def test_a_false_delete_is_not_found(self, ctx, bus) -> None:
        bus.delete_message_result = False
        with pytest.raises(OperationError) as exc:
            delete_message(ctx, Principal.supertoken(), DeleteMessageRequest("m1"))
        assert exc.value.kind == "not_found"


class TestPrivilegedGate:
    def test_a_scoped_principal_is_forbidden(self) -> None:
        with pytest.raises(OperationError) as exc:
            require_privileged(Principal.user("agent-1"))
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "Admin endpoints require a supertoken"

    @pytest.mark.parametrize(
        "principal", [Principal.supertoken(), Principal.admin(), Principal.no_auth()]
    )
    def test_every_privileged_kind_passes(self, principal) -> None:
        require_privileged(principal)


class TestCleanup:
    def test_requires_a_privileged_principal(self, ctx, bus) -> None:
        with pytest.raises(OperationError) as exc:
            cleanup(ctx, Principal.user("agent-1"), AdminCleanupRequest(pattern="*"))
        assert exc.value.kind == "forbidden"
        assert bus.deleted_channels == []

    def test_deletes_matching_channels_but_not_system_channels(self, ctx, bus) -> None:
        bus.channels_result = ["test:a", "_system:registry", "other"]
        result = cleanup(ctx, Principal.supertoken(), AdminCleanupRequest(pattern="test:*"))
        assert bus.deleted_channels == ["test:a"]
        assert result.channels_deleted == 1
        assert result.messages_deleted == 1
        assert result.channels == ["test:a"]

    def test_older_than_filters_by_last_activity(self, ctx, bus) -> None:
        bus.channels_result = ["old", "fresh"]
        bus.detail_result = [
            {"name": "old", "last_activity": "2026-01-01T00:00:00Z"},
            {"name": "fresh", "last_activity": "2026-06-01T00:00:00Z"},
        ]
        cleanup(
            ctx,
            Principal.supertoken(),
            AdminCleanupRequest(pattern="*", older_than="2026-03-01T00:00:00Z"),
        )
        assert bus.deleted_channels == ["old"]

    def test_backend_without_channel_detail_deletes_nothing(self, ctx, bus) -> None:
        bus.channels_result = ["old"]

        def unsupported() -> list[dict]:
            raise NotImplementedError

        bus.channels_detail = unsupported  # type: ignore[method-assign]
        cleanup(
            ctx,
            Principal.supertoken(),
            AdminCleanupRequest(pattern="*", older_than="2026-03-01T00:00:00Z"),
        )
        # No activity data means no channel can be shown to be old enough.
        assert bus.deleted_channels == []

    def test_a_backend_without_deletion_leaves_the_error_to_the_adapter(self, ctx, bus) -> None:
        bus.channels_result = ["test:a"]
        bus.delete_channel_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            cleanup(ctx, Principal.supertoken(), AdminCleanupRequest(pattern="test:*"))


class TestCompact:
    def test_requires_a_privileged_principal(self, ctx) -> None:
        with pytest.raises(OperationError) as exc:
            compact(ctx, Principal.user("agent-1"), AdminCompactRequest(channel="agents"))
        assert exc.value.kind == "forbidden"

    def test_returns_the_removed_count(self, ctx, bus) -> None:
        bus.compact_result = 7
        result = compact(
            ctx,
            Principal.supertoken(),
            AdminCompactRequest(channel="agents", max_messages=10, keep_latest_per_sender=True),
        )
        assert result.channel == "agents"
        assert result.messages_removed == 7

    def test_a_backend_without_compaction_leaves_the_error_to_the_adapter(self, ctx, bus) -> None:
        bus.compact_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            compact(ctx, Principal.supertoken(), AdminCompactRequest(channel="agents"))
