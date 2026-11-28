"""Operation-level tests for ``mansio_core.operations.queue`` (M6c)."""

from __future__ import annotations

import pytest
from mansio_core.contracts import QueueAckRequest, QueueClaimRequest, QueueStatusRequest
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.queue import ack, claim, status
from mansio_core.principal import Principal
from mansio_core.types import ClaimResult, Message


def _message(channel: str = "queue-lane", sender: str = "alice") -> Message:
    return Message(
        id="msg-1",
        channel=channel,
        sender=sender,
        msg_type="task",
        payload="work",
        timestamp="2026-01-01T00:00:00Z",
    )


def _claim_result(claimed_by: str) -> ClaimResult:
    return ClaimResult(
        message=_message(),
        status="claimed",
        claimed_by=claimed_by,
        claimed_at="2026-01-01T00:00:00Z",
    )


class _FakeAuthz:
    def __init__(self) -> None:
        self.claim_denial_result: str | None = None
        self.read_result = True
        self.calls: list[tuple] = []

    def claim_denial(self, user_id: str, channel: str) -> str | None:
        self.calls.append(("claim_denial", user_id, channel))
        return self.claim_denial_result

    def can_read(self, user_id: str, channel: str) -> bool:
        self.calls.append(("can_read", user_id, channel))
        return self.read_result


class _FakeBus:
    def __init__(self) -> None:
        self.claim_result: ClaimResult | None = _claim_result("alice")
        self.ack_result: ClaimResult | None = _claim_result("alice")
        self.status_result: dict | None = {"status": "claimed"}
        self.message: Message | None = _message()
        self.claim_calls: list[tuple] = []
        self.ack_calls: list[tuple] = []

    def get_message(self, message_id: str) -> Message | None:
        return self.message

    def queue_claim(self, channel: str, claimed_by: str, lease_seconds: int = 300):
        self.claim_calls.append((channel, claimed_by, lease_seconds))
        return self.claim_result

    def queue_ack(self, message_id: str, claimed_by: str):
        self.ack_calls.append((message_id, claimed_by))
        return self.ack_result

    def queue_status(self, message_id: str) -> dict | None:
        return self.status_result


@pytest.fixture()
def bus() -> _FakeBus:
    return _FakeBus()


@pytest.fixture()
def authz() -> _FakeAuthz:
    return _FakeAuthz()


@pytest.fixture()
def ctx(bus: _FakeBus, authz: _FakeAuthz) -> OperationContext:
    return OperationContext(bus=bus, authz=authz, token_store=None, allow_unauthenticated=False)


class TestClaim:
    def test_scoped_caller_claims_as_itself_even_if_the_body_says_otherwise(self, ctx, bus) -> None:
        claim(
            ctx, Principal.user("alice"), QueueClaimRequest(channel="queue-lane", claimed_by="bob")
        )
        assert bus.claim_calls == [("queue-lane", "alice", 300)]

    def test_scoped_caller_uses_claim_denial(self, ctx, bus, authz) -> None:
        authz.claim_denial_result = "Token for 'alice' cannot claim on 'queue-lane'"
        with pytest.raises(OperationError) as exc:
            claim(ctx, Principal.user("alice"), QueueClaimRequest(channel="queue-lane"))
        assert exc.value.kind == "forbidden"
        assert exc.value.message == "Token for 'alice' cannot claim on 'queue-lane'"
        assert bus.claim_calls == []

    def test_privileged_caller_must_name_the_claimer(self, ctx, bus) -> None:
        with pytest.raises(OperationError) as exc:
            claim(ctx, Principal.supertoken(), QueueClaimRequest(channel="queue-lane"))
        assert exc.value.kind == "bad_request"
        assert exc.value.message == "Missing 'claimed_by'"
        assert bus.claim_calls == []

    def test_privileged_caller_records_the_explicit_claimer(self, ctx, bus, authz) -> None:
        claim(
            ctx,
            Principal.supertoken(),
            QueueClaimRequest(channel="queue-lane", claimed_by="dispatcher"),
        )
        assert bus.claim_calls == [("queue-lane", "dispatcher", 300)]
        assert authz.calls == []

    def test_no_available_message_returns_none(self, ctx, bus) -> None:
        bus.claim_result = None
        assert claim(ctx, Principal.user("alice"), QueueClaimRequest(channel="queue-lane")) is None


class TestAck:
    def test_scoped_caller_acks_as_itself(self, ctx, bus) -> None:
        ack(ctx, Principal.user("alice"), QueueAckRequest(message_id="msg-1", claimed_by="bob"))
        assert bus.ack_calls == [("msg-1", "alice")]

    def test_a_hidden_message_is_not_acknowledged(self, ctx, bus, authz) -> None:
        authz.read_result = False
        result = ack(ctx, Principal.user("eve"), QueueAckRequest(message_id="msg-1"))
        assert result is None
        assert bus.ack_calls == []

    def test_privileged_caller_needs_claimed_by(self, ctx, bus) -> None:
        with pytest.raises(OperationError) as exc:
            ack(ctx, Principal.supertoken(), QueueAckRequest(message_id="msg-1"))
        assert exc.value.kind == "bad_request"
        assert exc.value.message == "Missing 'claimed_by'"

    def test_a_failed_ack_returns_none(self, ctx, bus) -> None:
        bus.ack_result = None
        assert ack(ctx, Principal.user("alice"), QueueAckRequest(message_id="msg-1")) is None


class TestStatus:
    def test_returns_the_state(self, ctx, bus) -> None:
        assert status(ctx, Principal.user("alice"), QueueStatusRequest(message_id="msg-1")) == {
            "status": "claimed"
        }

    def test_a_hidden_message_looks_like_it_has_no_state(self, ctx, bus, authz) -> None:
        authz.read_result = False
        assert status(ctx, Principal.user("eve"), QueueStatusRequest(message_id="msg-1")) is None

    def test_a_missing_message_has_no_state(self, ctx, bus) -> None:
        bus.status_result = None
        assert status(ctx, Principal.supertoken(), QueueStatusRequest(message_id="msg-9")) is None
