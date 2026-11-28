"""Operation-level tests for ``mansio_core.operations.streams`` (M6d-1)."""

from __future__ import annotations

from typing import Any

import pytest
from mansio_core.authz import DM_NAME_EXPECTED
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.streams import (
    StreamRequest,
    allows,
    replay,
    subscribe,
    unsubscribe,
    validate,
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
        self.unreadable: set[str] = set()
        self.calls: list[tuple] = []

    def can_read(self, user_id: str, channel: str) -> bool:
        self.calls.append(("can_read", user_id, channel))
        return channel not in self.unreadable


class _FakeBus:
    def __init__(self) -> None:
        self.subscriptions: dict[str, tuple[str, Any]] = {}
        self.unsubscribed: list[str] = []
        self.query_result: dict[str, list[Message]] = {}
        self.query_calls: list[tuple] = []

    def subscribe(self, channel: str, callback: Any) -> str:
        sub_id = f"sub-{len(self.subscriptions) + 1}"
        self.subscriptions[sub_id] = (channel, callback)
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        self.unsubscribed.append(sub_id)

    def query(self, channel: str, after: str, limit: int) -> list[Message]:
        self.query_calls.append((channel, after, limit))
        return self.query_result.get(channel, [])

    def publish_to(self, channel: str, message: Message) -> None:
        """Deliver *message* to the callbacks of *channel*."""
        for sub_channel, callback in self.subscriptions.values():
            if sub_channel == channel:
                callback(message)


@pytest.fixture()
def bus() -> _FakeBus:
    return _FakeBus()


@pytest.fixture()
def authz() -> _FakeAuthz:
    return _FakeAuthz()


@pytest.fixture()
def ctx(bus: _FakeBus, authz: _FakeAuthz) -> OperationContext:
    return OperationContext(bus=bus, authz=authz, token_store=None, allow_unauthenticated=False)


class TestValidate:
    def test_rejects_a_malformed_dm_name(self, ctx, authz) -> None:
        with pytest.raises(OperationError) as exc:
            validate(ctx, Principal.user("agent-1"), StreamRequest(channels=["dm:onlyone"]))
        assert exc.value.kind == "bad_request"
        assert exc.value.message == f"{DM_NAME_EXPECTED} (got 'dm:onlyone')"
        assert authz.calls == []

    def test_rejects_a_channel_the_caller_cannot_read(self, ctx, authz) -> None:
        authz.unreadable = {"secret-room"}
        with pytest.raises(OperationError) as exc:
            validate(
                ctx,
                Principal.user("agent-1"),
                StreamRequest(channels=["open-room", "secret-room"]),
            )
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "Token for 'agent-1' cannot subscribe to: secret-room"

    @pytest.mark.parametrize(
        "principal", [Principal.supertoken(), Principal.admin(), Principal.no_auth()]
    )
    def test_privileged_callers_skip_the_read_check(self, ctx, authz, principal) -> None:
        validate(ctx, principal, StreamRequest(channels=["secret-room"]))
        assert authz.calls == []


class TestAllows:
    def test_privileged_callers_see_everything(self) -> None:
        assert allows(Principal.supertoken(), "secret-room", "anyone", exclude_own=False)

    def test_a_scoped_caller_sees_a_public_channel(self) -> None:
        assert allows(Principal.user("agent-1"), "open-room", "agent-2", exclude_own=False)

    def test_a_scoped_caller_sees_their_own_private_messages(self) -> None:
        assert allows(Principal.user("agent-1"), "notebook:agent-1", "agent-1", exclude_own=False)

    def test_a_scoped_caller_does_not_see_another_notebook(self) -> None:
        assert not allows(
            Principal.user("agent-1"), "notebook:agent-2", "agent-3", exclude_own=False
        )

    def test_exclude_own_drops_the_callers_messages(self) -> None:
        assert not allows(Principal.user("agent-1"), "open-room", "agent-1", exclude_own=True)
        assert allows(Principal.user("agent-1"), "open-room", "agent-2", exclude_own=True)

    def test_exclude_own_has_no_effect_without_a_user(self) -> None:
        assert allows(Principal.supertoken(), "open-room", "agent-1", exclude_own=True)


class TestSubscribe:
    def test_validates_first(self, ctx, bus) -> None:
        with pytest.raises(OperationError):
            subscribe(
                ctx,
                Principal.user("agent-1"),
                StreamRequest(channels=["dm:onlyone"]),
                lambda channel, message: None,
            )
        assert bus.subscriptions == {}

    def test_subscribes_every_channel_and_filters(self, ctx, bus) -> None:
        delivered: list[tuple[str, str]] = []
        sub_ids = subscribe(
            ctx,
            Principal.user("agent-1"),
            StreamRequest(channels=["open-room", "notebook:agent-2"]),
            lambda channel, message: delivered.append((channel, message.id)),
        )

        assert len(sub_ids) == 2
        assert {channel for channel, _ in bus.subscriptions.values()} == {
            "open-room",
            "notebook:agent-2",
        }

        bus.publish_to("open-room", _message("m1", "open-room", "agent-2"))
        bus.publish_to("notebook:agent-2", _message("m2", "notebook:agent-2", "agent-3"))
        bus.publish_to("notebook:agent-2", _message("m3", "notebook:agent-2", "agent-1"))

        assert delivered == [("open-room", "m1"), ("notebook:agent-2", "m3")]

    def test_exclude_own_reaches_the_live_filter(self, ctx, bus) -> None:
        delivered: list[str] = []
        subscribe(
            ctx,
            Principal.user("agent-1"),
            StreamRequest(channels=["open-room"], exclude_own=True),
            lambda channel, message: delivered.append(message.id),
        )
        bus.publish_to("open-room", _message("m1", "open-room", "agent-1"))
        bus.publish_to("open-room", _message("m2", "open-room", "agent-2"))
        assert delivered == ["m2"]


class TestUnsubscribe:
    def test_drops_every_subscription(self, ctx, bus) -> None:
        unsubscribe(ctx, ["sub-1", "sub-2"])
        assert bus.unsubscribed == ["sub-1", "sub-2"]


class TestReplay:
    def test_queries_each_channel_in_order(self, ctx, bus) -> None:
        bus.query_result = {
            "first": [_message("m1", "first", "agent-1")],
            "second": [_message("m2", "second", "agent-1")],
        }
        result = replay(
            ctx,
            Principal.supertoken(),
            StreamRequest(channels=["first", "second"]),
            "last-id",
            500,
        )
        assert [channel for channel, _, _ in result] == ["first", "second"]
        assert bus.query_calls == [("first", "last-id", 500), ("second", "last-id", 500)]

    def test_truncation_uses_the_raw_count_before_filtering(self, ctx, bus) -> None:
        bus.query_result = {"private": [_message("m1", "notebook:agent-2", "agent-3")] * 2}
        result = replay(
            ctx,
            Principal.user("agent-1"),
            StreamRequest(channels=["private"]),
            "last-id",
            2,
        )
        channel, messages, truncated = result[0]
        assert channel == "private"
        assert messages == []
        assert truncated is True

    def test_applies_the_same_visibility_filter(self, ctx, bus) -> None:
        bus.query_result = {
            "notebook:agent-2": [
                _message("m1", "notebook:agent-2", "agent-1"),
                _message("m2", "notebook:agent-2", "agent-3"),
            ]
        }
        _, messages, truncated = replay(
            ctx,
            Principal.user("agent-1"),
            StreamRequest(channels=["notebook:agent-2"]),
            "last-id",
            500,
        )[0]
        assert [message.id for message in messages] == ["m1"]
        assert truncated is False

    def test_exclude_own_applies_to_replay(self, ctx, bus) -> None:
        bus.query_result = {"open-room": [_message("m1", "open-room", "agent-1")]}
        _, messages, _ = replay(
            ctx,
            Principal.user("agent-1"),
            StreamRequest(channels=["open-room"], exclude_own=True),
            "last-id",
            500,
        )[0]
        assert messages == []
