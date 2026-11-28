"""Operation-level tests for ``mansio_core.operations.channels`` (M6c)."""

from __future__ import annotations

import pytest
from mansio_core.contracts import ChannelCreateRequest, RegistryLookupRequest
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.channels import (
    channel_meta,
    create_channel,
    delete_channel,
    list_channels,
    registry_lookup,
)
from mansio_core.principal import Principal
from mansio_core.types import ChannelMeta


def _meta(name: str = "team", owner: str = "agent-1") -> ChannelMeta:
    return ChannelMeta(
        name=name,
        owner=owner,
        visibility="public",
        created_at="2026-01-01T00:00:00Z",
    )


class _FakeAuthz:
    def __init__(self) -> None:
        self.hidden: set[str] = set()
        self.create_denial_result: str | None = None
        self.read_result = True
        self.delete_denial_result: str | None = None
        self.calls: list[tuple] = []

    def can_list_channels(self, user_id: str, names: list[str]) -> dict[str, bool]:
        self.calls.append(("can_list_channels", user_id, tuple(names)))
        return {name: name not in self.hidden for name in names}

    def create_denial(self, user_id: str, channel: str) -> str | None:
        self.calls.append(("create_denial", user_id, channel))
        return self.create_denial_result

    def can_read(self, user_id: str, channel: str) -> bool:
        self.calls.append(("can_read", user_id, channel))
        return self.read_result

    def delete_channel_denial(self, user_id: str, channel: str) -> str | None:
        self.calls.append(("delete_channel_denial", user_id, channel))
        return self.delete_denial_result


class _FakeTokenStore:
    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known or set()

    def user_exists(self, user_id: str) -> bool:
        return user_id in self.known


class _FakeBus:
    def __init__(self) -> None:
        self.names = ["open-room", "secret-room"]
        self.details = [
            {"name": "open-room", "owner": "agent-1"},
            {"name": "secret-room", "owner": "agent-2"},
        ]
        self.meta_result: ChannelMeta | None = _meta()
        self.meta_error: Exception | None = None
        self.create_result = _meta()
        self.create_error: Exception | None = None
        self.created: tuple | None = None
        self.delete_result = 3
        self.delete_error: Exception | None = None

    def channels(self, detail: bool = False) -> list[str]:
        return self.names

    def channels_detail(self) -> list[dict]:
        return self.details

    def create_channel(self, name: str, owner: str, visibility: str = "public") -> ChannelMeta:
        if self.create_error is not None:
            raise self.create_error
        self.created = (name, owner, visibility)
        return self.create_result

    def get_channel_meta(self, channel: str) -> ChannelMeta | None:
        if self.meta_error is not None:
            raise self.meta_error
        return self.meta_result

    def delete_channel(self, channel: str) -> int:
        if self.delete_error is not None:
            raise self.delete_error
        return self.delete_result


@pytest.fixture()
def bus() -> _FakeBus:
    return _FakeBus()


@pytest.fixture()
def authz() -> _FakeAuthz:
    return _FakeAuthz()


@pytest.fixture()
def ctx(bus: _FakeBus, authz: _FakeAuthz) -> OperationContext:
    return OperationContext(bus=bus, authz=authz, token_store=None, allow_unauthenticated=False)


class TestListChannels:
    def test_privileged_names_are_unfiltered(self, ctx, bus, authz) -> None:
        assert list_channels(ctx, Principal.supertoken(), detail=False) == bus.names
        assert authz.calls == []

    def test_privileged_detail_is_unfiltered(self, ctx, bus, authz) -> None:
        assert list_channels(ctx, Principal.admin(), detail=True) == bus.details
        assert authz.calls == []

    def test_scoped_names_use_the_list_rule(self, ctx, bus, authz) -> None:
        authz.hidden = {"secret-room"}
        assert list_channels(ctx, Principal.user("agent-1"), detail=False) == ["open-room"]

    def test_scoped_detail_uses_the_list_rule(self, ctx, bus, authz) -> None:
        authz.hidden = {"secret-room"}
        rows = list_channels(ctx, Principal.user("agent-1"), detail=True)
        assert [row["name"] for row in rows] == ["open-room"]


class TestCreateChannel:
    def test_scoped_caller_forces_the_token_as_owner(self, ctx, bus, authz) -> None:
        meta = create_channel(
            ctx, Principal.user("agent-1"), ChannelCreateRequest(name="team", owner="agent-2")
        )
        assert meta is bus.create_result
        assert bus.created == ("team", "agent-1", "public")

    def test_a_create_denial_is_forbidden(self, ctx, bus, authz) -> None:
        authz.create_denial_result = "no create for you"
        with pytest.raises(OperationError) as exc:
            create_channel(ctx, Principal.user("agent-1"), ChannelCreateRequest(name="team"))
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "no create for you"
        assert bus.created is None

    def test_privileged_caller_needs_an_explicit_owner(self, ctx, bus) -> None:
        with pytest.raises(OperationError) as exc:
            create_channel(ctx, Principal.supertoken(), ChannelCreateRequest(name="team"))
        assert exc.value.kind == "bad_request"
        assert exc.value.message == "'owner' is required for supertoken/no-auth"
        assert bus.created is None

    def test_privileged_caller_records_the_given_owner(self, ctx, bus) -> None:
        create_channel(
            ctx, Principal.supertoken(), ChannelCreateRequest(name="team", owner="operator")
        )
        assert bus.created == ("team", "operator", "public")

    def test_a_malformed_dm_name_is_bad_request(self, ctx, bus) -> None:
        with pytest.raises(OperationError) as exc:
            create_channel(ctx, Principal.supertoken(), ChannelCreateRequest(name="dm:onlyone"))
        assert exc.value.kind == "bad_request"
        assert bus.created is None

    def test_a_bus_value_error_is_conflict(self, ctx, bus) -> None:
        bus.create_error = ValueError("channel 'team' already exists")
        with pytest.raises(OperationError) as exc:
            create_channel(
                ctx, Principal.supertoken(), ChannelCreateRequest(name="team", owner="operator")
            )
        assert exc.value.kind == "conflict"
        assert exc.value.message == "channel 'team' already exists"

    def test_backend_without_channel_management_leaves_the_error_to_the_adapter(
        self, ctx, bus
    ) -> None:
        bus.create_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            create_channel(
                ctx, Principal.supertoken(), ChannelCreateRequest(name="team", owner="operator")
            )


class TestChannelMeta:
    def test_returns_the_metadata(self, ctx, bus) -> None:
        assert channel_meta(ctx, Principal.user("agent-1"), "team") is bus.meta_result

    def test_a_read_denial_is_the_missing_channel_body(self, ctx, bus, authz) -> None:
        authz.read_result = False
        with pytest.raises(OperationError) as exc:
            channel_meta(ctx, Principal.user("agent-1"), "secret-room")
        assert exc.value.kind == "not_found"
        assert exc.value.message == "Channel 'secret-room' not found"

    def test_a_missing_channel_is_not_found(self, ctx, bus) -> None:
        bus.meta_result = None
        with pytest.raises(OperationError) as exc:
            channel_meta(ctx, Principal.supertoken(), "ghost-lane")
        assert exc.value.kind == "not_found"
        assert exc.value.message == "Channel 'ghost-lane' not found"

    def test_backend_without_channel_management_leaves_the_error_to_the_adapter(
        self, ctx, bus
    ) -> None:
        bus.meta_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            channel_meta(ctx, Principal.supertoken(), "team")


class TestDeleteChannel:
    def test_scoped_callers_use_the_delete_rule(self, ctx, bus, authz) -> None:
        authz.delete_denial_result = "Scoped tokens cannot delete shared channels"
        with pytest.raises(OperationError) as exc:
            delete_channel(ctx, Principal.user("agent-1"), "ops")
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "Scoped tokens cannot delete shared channels"

    def test_a_zero_count_is_not_found(self, ctx, bus) -> None:
        bus.delete_result = 0
        with pytest.raises(OperationError) as exc:
            delete_channel(ctx, Principal.supertoken(), "ghost-lane")
        assert exc.value.kind == "not_found"
        assert exc.value.message == "Channel 'ghost-lane' not found or already empty"

    def test_returns_the_message_count(self, ctx, bus) -> None:
        assert delete_channel(ctx, Principal.supertoken(), "ops") == 3

    def test_backend_without_deletion_leaves_the_error_to_the_adapter(self, ctx, bus) -> None:
        bus.delete_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            delete_channel(ctx, Principal.supertoken(), "ops")


class TestRegistryLookup:
    def test_without_a_token_store_nothing_is_found(self, ctx) -> None:
        assert (
            registry_lookup(ctx, Principal.supertoken(), RegistryLookupRequest("agent-1")) is False
        )

    def test_asks_the_token_store(self, bus, authz) -> None:
        ctx = OperationContext(
            bus=bus,
            authz=authz,
            token_store=_FakeTokenStore({"agent-1"}),
            allow_unauthenticated=False,
        )
        assert (
            registry_lookup(ctx, Principal.supertoken(), RegistryLookupRequest("agent-1")) is True
        )
        assert (
            registry_lookup(ctx, Principal.supertoken(), RegistryLookupRequest("agent-9")) is False
        )
