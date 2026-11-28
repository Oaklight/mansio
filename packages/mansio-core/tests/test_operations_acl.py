"""Operation-level tests for ``mansio_core.operations.acl`` (M6c)."""

from __future__ import annotations

from datetime import datetime

import pytest
from mansio_core.contracts import ACLAddRequest, ACLSetRequest
from mansio_core.contracts.bus import ACLEntrySpec
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.acl import (
    add_acl_entry,
    get_acl,
    remove_acl_entry,
    require_manage,
    set_acl,
)
from mansio_core.principal import Principal
from mansio_core.types import ACLEntry, ChannelMeta


class _FakeAuthz:
    def __init__(self) -> None:
        self.can_manage = True
        self.calls: list[tuple] = []

    def can_manage_acl(self, user_id: str, channel: str) -> bool:
        self.calls.append(("can_manage_acl", user_id, channel))
        return self.can_manage


class _FakeBus:
    def __init__(self) -> None:
        self.meta_result: ChannelMeta | None = ChannelMeta(
            name="ops",
            owner="agent-1",
            visibility="public",
            created_at="2026-01-01T00:00:00Z",
        )
        self.entries: list[ACLEntry] = []
        self.get_result: list[ACLEntry] = []
        self.get_error: Exception | None = None
        self.set_error: Exception | None = None
        self.add_error: Exception | None = None
        self.remove_result = True
        self.remove_error: Exception | None = None
        self.set_calls: list[list[ACLEntry]] = []
        self.add_calls: list[ACLEntry] = []

    def get_channel_meta(self, channel: str) -> ChannelMeta | None:
        return self.meta_result

    def get_acl(self, channel: str) -> list[ACLEntry]:
        if self.get_error is not None:
            raise self.get_error
        return self.get_result

    def set_acl(self, channel: str, entries: list[ACLEntry]) -> None:
        if self.set_error is not None:
            raise self.set_error
        self.set_calls.append(entries)

    def add_acl_entry(self, entry: ACLEntry) -> None:
        if self.add_error is not None:
            raise self.add_error
        self.add_calls.append(entry)

    def remove_acl_entry(self, channel: str, user_id: str) -> bool:
        if self.remove_error is not None:
            raise self.remove_error
        return self.remove_result


@pytest.fixture()
def bus() -> _FakeBus:
    return _FakeBus()


@pytest.fixture()
def authz() -> _FakeAuthz:
    return _FakeAuthz()


@pytest.fixture()
def ctx(bus: _FakeBus, authz: _FakeAuthz) -> OperationContext:
    return OperationContext(bus=bus, authz=authz, token_store=None, allow_unauthenticated=False)


class TestManageGate:
    def test_a_denied_scoped_caller_is_forbidden(self, ctx, authz) -> None:
        authz.can_manage = False
        with pytest.raises(OperationError) as exc:
            require_manage(ctx, Principal.user("bob"), "ops")
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "Agent 'bob' lacks manage permission on 'ops'"

    @pytest.mark.parametrize(
        "principal", [Principal.supertoken(), Principal.admin(), Principal.no_auth()]
    )
    def test_privileged_callers_bypass(self, ctx, authz, principal) -> None:
        require_manage(ctx, principal, "ops")
        assert authz.calls == []


class TestGetAcl:
    def test_returns_the_entries(self, ctx, bus) -> None:
        entry = ACLEntry(channel="ops", user_id="bob", permission="viewer")
        bus.get_result = [entry]
        assert get_acl(ctx, Principal.user("agent-1"), "ops") == [entry]

    def test_the_gate_runs_first(self, ctx, bus, authz) -> None:
        authz.can_manage = False
        with pytest.raises(OperationError):
            get_acl(ctx, Principal.user("bob"), "ops")
        assert bus.get_result == []

    def test_backend_without_channel_management_leaves_the_error_to_the_adapter(
        self, ctx, bus
    ) -> None:
        bus.get_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            get_acl(ctx, Principal.supertoken(), "ops")


class TestSetAcl:
    def _dto(self) -> ACLSetRequest:
        return ACLSetRequest(
            entries=[
                ACLEntrySpec(user_id="bob", permission="viewer"),
                ACLEntrySpec(user_id="carol", permission="member"),
            ]
        )

    def test_records_the_scoped_caller_as_grantor(self, ctx, bus) -> None:
        entries = set_acl(ctx, Principal.user("alice"), "ops", self._dto())

        assert [entry.user_id for entry in entries] == ["bob", "carol"]
        assert {entry.channel for entry in entries} == {"ops"}
        assert {entry.granted_by for entry in entries} == {"alice"}
        for entry in entries:
            datetime.fromisoformat(entry.granted_at)
        assert bus.set_calls == [entries]

    def test_privileged_writes_record_no_grantor(self, ctx, bus) -> None:
        entries = set_acl(ctx, Principal.supertoken(), "ops", self._dto())
        assert {entry.granted_by for entry in entries} == {None}

    def test_a_missing_channel_is_not_found(self, ctx, bus) -> None:
        bus.meta_result = None
        with pytest.raises(OperationError) as exc:
            set_acl(ctx, Principal.user("alice"), "ghost-lane", self._dto())
        assert exc.value.kind == "not_found"
        assert exc.value.message == "Channel 'ghost-lane' not found"
        assert bus.set_calls == []

    def test_backend_without_channel_management_leaves_the_error_to_the_adapter(
        self, ctx, bus
    ) -> None:
        bus.set_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            set_acl(ctx, Principal.supertoken(), "ops", self._dto())


class TestAddAclEntry:
    def test_records_the_entry(self, ctx, bus) -> None:
        entry = add_acl_entry(
            ctx, Principal.user("alice"), "ops", ACLAddRequest(user_id="bob", permission="viewer")
        )
        assert entry == ACLEntry(
            channel="ops",
            user_id="bob",
            permission="viewer",
            granted_at=entry.granted_at,
            granted_by="alice",
        )
        assert bus.add_calls == [entry]

    def test_privileged_writes_record_no_grantor(self, ctx, bus) -> None:
        entry = add_acl_entry(
            ctx, Principal.supertoken(), "ops", ACLAddRequest(user_id="bob", permission="viewer")
        )
        assert entry.granted_by is None

    def test_a_missing_channel_is_not_found(self, ctx, bus) -> None:
        bus.meta_result = None
        with pytest.raises(OperationError) as exc:
            add_acl_entry(
                ctx,
                Principal.user("alice"),
                "ghost-lane",
                ACLAddRequest(user_id="bob", permission="viewer"),
            )
        assert exc.value.kind == "not_found"
        assert bus.add_calls == []

    def test_backend_without_channel_management_leaves_the_error_to_the_adapter(
        self, ctx, bus
    ) -> None:
        bus.add_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            add_acl_entry(
                ctx,
                Principal.supertoken(),
                "ops",
                ACLAddRequest(user_id="bob", permission="viewer"),
            )


class TestRemoveAclEntry:
    def test_removes_an_entry(self, ctx, bus) -> None:
        remove_acl_entry(ctx, Principal.user("alice"), "ops", "bob")

    def test_a_missing_entry_is_not_found(self, ctx, bus) -> None:
        bus.remove_result = False
        with pytest.raises(OperationError) as exc:
            remove_acl_entry(ctx, Principal.user("alice"), "ops", "bob")
        assert exc.value.kind == "not_found"
        assert exc.value.message == "No ACL entry for 'bob' on 'ops'"

    def test_backend_without_channel_management_leaves_the_error_to_the_adapter(
        self, ctx, bus
    ) -> None:
        bus.remove_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            remove_acl_entry(ctx, Principal.supertoken(), "ops", "bob")
