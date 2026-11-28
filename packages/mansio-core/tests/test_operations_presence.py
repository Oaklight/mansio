"""Operation-level tests for ``mansio_core.operations.presence`` (M6c)."""

from __future__ import annotations

import pytest
from mansio_core.contracts import PresenceHeartbeatRequest, PresenceListRequest
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.presence import get, list_presence, touch
from mansio_core.principal import Principal
from mansio_core.types import UserPresence


def _presence(user_id: str = "alice") -> UserPresence:
    return UserPresence(
        user_id=user_id,
        status="online",
        last_seen="2026-01-01T00:00:00Z",
    )


class _FakeBus:
    def __init__(self) -> None:
        self.heartbeats: list[tuple] = []
        self.heartbeat_error: Exception | None = None
        self.users_result: list[UserPresence] = [_presence()]
        self.users_error: Exception | None = None
        self.user_result: UserPresence | None = _presence()
        self.user_error: Exception | None = None

    def heartbeat(self, user_id: str, metadata: dict | None = None) -> None:
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        self.heartbeats.append((user_id, metadata))

    def users(self, timeout_seconds: int = 120) -> list[UserPresence]:
        if self.users_error is not None:
            raise self.users_error
        return self.users_result

    def user_status(self, user_id: str, timeout_seconds: int = 120) -> UserPresence | None:
        if self.user_error is not None:
            raise self.user_error
        return self.user_result


class _FakeAuthz:
    def can_read(self, user_id: str, channel: str) -> bool:
        return True


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


class TestTouch:
    def test_scoped_caller_reports_its_own_presence(self, ctx, bus) -> None:
        touch(ctx, Principal.user("alice"), PresenceHeartbeatRequest(user_id="alice"))
        assert bus.heartbeats == [("alice", None)]

    def test_scoped_caller_cannot_report_another_user(self, ctx, bus) -> None:
        with pytest.raises(OperationError) as exc:
            touch(ctx, Principal.user("alice"), PresenceHeartbeatRequest(user_id="bob"))
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "Token bound to 'alice', cannot heartbeat as 'bob'"
        assert bus.heartbeats == []

    @pytest.mark.parametrize(
        "principal", [Principal.supertoken(), Principal.admin(), Principal.no_auth()]
    )
    def test_privileged_callers_may_report_anyone(self, ctx, bus, principal) -> None:
        touch(ctx, principal, PresenceHeartbeatRequest(user_id="agent-z", metadata={"k": "v"}))
        assert bus.heartbeats == [("agent-z", {"k": "v"})]

    def test_backend_without_presence_leaves_the_error_to_the_adapter(self, ctx, bus) -> None:
        bus.heartbeat_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            touch(ctx, Principal.user("alice"), PresenceHeartbeatRequest(user_id="alice"))


class TestListPresence:
    def test_returns_every_agent(self, ctx, bus) -> None:
        assert list_presence(ctx, Principal.supertoken(), PresenceListRequest()) == bus.users_result

    def test_backend_without_presence_leaves_the_error_to_the_adapter(self, ctx, bus) -> None:
        bus.users_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            list_presence(ctx, Principal.supertoken(), PresenceListRequest())


class TestGetPresence:
    def test_returns_one_agent(self, ctx) -> None:
        assert get(ctx, Principal.supertoken(), "alice", PresenceListRequest()) == _presence()

    def test_an_unknown_agent_is_not_found(self, ctx, bus) -> None:
        bus.user_result = None
        with pytest.raises(OperationError) as exc:
            get(ctx, Principal.supertoken(), "ghost", PresenceListRequest())
        assert exc.value.kind == "not_found"
        assert exc.value.message == "agent not found"

    def test_backend_without_presence_leaves_the_error_to_the_adapter(self, ctx, bus) -> None:
        bus.user_error = NotImplementedError
        with pytest.raises(NotImplementedError):
            get(ctx, Principal.supertoken(), "alice", PresenceListRequest())
