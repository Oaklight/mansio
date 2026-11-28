"""Delivery semantics: pull/queue/stream guarantees and wait behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from mansio_client.delivery import (
    DEFAULT_WAIT_SECONDS,
    MAX_WAIT_SECONDS,
    WAIT_POLL_INTERVAL_SECONDS,
    AgentDelivery,
)
from mansio_client.delivery import agent as delivery_agent


def _message(message_id: str) -> Any:
    return SimpleNamespace(id=message_id, sender="agent-1", payload="hello", channel="general")


class _FakeClient:
    """Model the cursor: a poll moves it, a read does not."""

    def __init__(self, messages: list[Any] | None = None) -> None:
        self.messages = list(messages or [])
        self.cursor = 0
        self.list_calls: list[bool] = []
        self.polls: list[tuple[str, bool]] = []
        self.reads: list[dict[str, Any]] = []
        self.unread_calls: list[bool] = []
        self.queue_calls: list[tuple[Any, ...]] = []
        self.subscribe_calls: list[tuple[Any, ...]] = []
        self.warnings: tuple = ()
        self.claim_result: Any = None
        self.ack_result: Any = None
        self.status_result: Any = None

    def channel_list(self, *, detail: bool = False) -> list[str]:
        self.list_calls.append(detail)
        return ["general", "_system:cursors:agent-1"]

    def channel_poll(self, channel: str, *, include_own: bool = False) -> list[Any]:
        self.polls.append((channel, include_own))
        pending = self.messages[self.cursor :]
        self.cursor = len(self.messages)
        return pending

    def channel_read(
        self,
        channel: str,
        *,
        limit: int = 10,
        after: str | None = None,
        order: str = "newest",
        thread_id: str | None = None,
        intent: str | None = None,
        include_own: bool = True,
    ) -> list[Any]:
        self.reads.append(
            {
                "channel": channel,
                "limit": limit,
                "after": after,
                "order": order,
                "thread_id": thread_id,
                "intent": intent,
                "include_own": include_own,
            }
        )
        return list(self.messages)

    def check_unread(self, *, include_own: bool = False) -> dict[str, int]:
        self.unread_calls.append(include_own)
        return {"general": len(self.messages) - self.cursor}

    def queue_publish(
        self,
        channel: str,
        content: str,
        *,
        msg_type: str = "task",
        metadata: dict | None = None,
    ) -> str:
        self.queue_calls.append(("publish", channel, content, msg_type, metadata))
        return "msg-q1"

    def queue_claim(self, channel: str, *, lease_seconds: int = 300) -> Any:
        self.queue_calls.append(("claim", channel, lease_seconds))
        return self.claim_result

    def queue_ack(self, message_id: str) -> Any:
        self.queue_calls.append(("ack", message_id))
        return self.ack_result

    def queue_status(self, message_id: str) -> Any:
        self.queue_calls.append(("status", message_id))
        return self.status_result

    def subscribe(
        self,
        channel: str,
        callback: Any,
        on_error: Any = None,
        *,
        include_own: bool = False,
        coalesce_ms: int = 0,
    ) -> str:
        self.subscribe_calls.append((channel, callback, on_error, include_own, coalesce_ms))
        return "sub-1"

    @property
    def recent_warnings(self) -> tuple:
        return self.warnings


class _Clock:
    """A deterministic replacement for :mod:`time` in the delivery module."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    replacement = _Clock()
    monkeypatch.setattr(delivery_agent, "time", replacement)
    return replacement


def _delivery(messages: list[Any] | None = None) -> tuple[_FakeClient, AgentDelivery]:
    client = _FakeClient(messages)
    return client, AgentDelivery(client)


def test_pull_advances_the_cursor() -> None:
    client, delivery = _delivery([_message("m1"), _message("m2")])

    assert [m.id for m in delivery.pull("general")] == ["m1", "m2"]
    assert client.cursor == 2
    assert delivery.pull("general") == []
    assert client.polls == [("general", False), ("general", False)]


def test_pull_passes_include_own() -> None:
    client, delivery = _delivery()
    delivery.pull("general", include_own=True)
    assert client.polls == [("general", True)]


def test_read_does_not_advance_the_cursor() -> None:
    client, delivery = _delivery([_message("m1"), _message("m2")])

    assert [m.id for m in delivery.read("general")] == ["m1", "m2"]
    assert client.cursor == 0
    assert [m.id for m in delivery.read("general")] == ["m1", "m2"]
    assert client.cursor == 0
    assert [m.id for m in delivery.pull("general")] == ["m1", "m2"]
    assert client.reads[-1] == {
        "channel": "general",
        "limit": 10,
        "after": None,
        "order": "newest",
        "thread_id": None,
        "intent": None,
        "include_own": False,
    }


def test_read_forwards_every_filter() -> None:
    client, delivery = _delivery()
    delivery.read(
        "dev",
        limit=5,
        after="m0",
        order="oldest",
        thread_id="t1",
        intent="FYI_ONLY",
        include_own=True,
    )
    assert client.reads == [
        {
            "channel": "dev",
            "limit": 5,
            "after": "m0",
            "order": "oldest",
            "thread_id": "t1",
            "intent": "FYI_ONLY",
            "include_own": True,
        }
    ]


def test_check_unread_does_not_advance_the_cursor() -> None:
    client, delivery = _delivery([_message("m1"), _message("m2")])

    assert delivery.check_unread() == {"general": 2}
    assert client.unread_calls == [False]
    assert client.cursor == 0
    assert [m.id for m in delivery.pull("general")] == ["m1", "m2"]


def test_channels_does_not_advance_any_cursor() -> None:
    client, delivery = _delivery([_message("m1")])

    assert delivery.channels() == ["general", "_system:cursors:agent-1"]
    assert delivery.channels(detail=True) == ["general", "_system:cursors:agent-1"]
    assert client.list_calls == [False, True]
    assert client.cursor == 0


@pytest.mark.parametrize("value", [True, False, "5", None, [1]])
def test_wait_rejects_a_non_number_timeout(value: Any) -> None:
    _, delivery = _delivery()
    with pytest.raises(ValueError) as exc:
        delivery.wait("general", value)
    assert str(exc.value) == "timeout_seconds must be a number"


def test_wait_rejects_a_negative_timeout() -> None:
    _, delivery = _delivery()
    with pytest.raises(ValueError) as exc:
        delivery.wait("general", -1)
    assert str(exc.value) == "timeout_seconds must be non-negative"


def test_wait_rejects_a_timeout_above_the_maximum() -> None:
    _, delivery = _delivery()
    with pytest.raises(ValueError) as exc:
        delivery.wait("general", MAX_WAIT_SECONDS + 1)
    assert str(exc.value) == f"timeout_seconds must be at most {MAX_WAIT_SECONDS}"


def test_wait_returns_as_soon_as_messages_arrive(clock: _Clock) -> None:
    class _Scripted(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.batches = [[], [_message("m1")]]

        def channel_poll(self, channel: str, *, include_own: bool = False) -> list[Any]:
            self.polls.append((channel, include_own))
            return self.batches.pop(0)

    client = _Scripted()

    result = AgentDelivery(client).wait("agents", 5)

    assert result.timed_out is False
    assert [m.id for m in result.messages] == ["m1"]
    assert clock.sleeps == [WAIT_POLL_INTERVAL_SECONDS]
    assert client.polls == [("agents", False), ("agents", False)]


def test_wait_times_out_exactly_at_the_deadline(clock: _Clock) -> None:
    client, delivery = _delivery()

    result = delivery.wait("agents", 3)

    assert result.timed_out is True
    assert result.messages == []
    assert clock.sleeps == [1.0, 1.0, 1.0]
    assert clock.now == 3.0
    assert len(client.polls) == 4


def test_wait_uses_the_default_timeout(clock: _Clock) -> None:
    _, delivery = _delivery()

    delivery.wait("agents")

    assert clock.now == DEFAULT_WAIT_SECONDS
    assert len(clock.sleeps) == DEFAULT_WAIT_SECONDS


def test_wait_zero_polls_once(clock: _Clock) -> None:
    client, delivery = _delivery()

    result = delivery.wait("agents", 0)

    assert result.timed_out is True
    assert clock.sleeps == []
    assert len(client.polls) == 1


def test_wait_passes_include_own() -> None:
    client, delivery = _delivery([_message("m1")])

    delivery.wait("agents", 5, include_own=True)

    assert client.polls == [("agents", True)]


def test_queue_methods_delegate() -> None:
    client, delivery = _delivery()
    claimed = SimpleNamespace(status="claimed")
    completed = SimpleNamespace(status="completed")
    state = {"status": "claimed"}
    client.claim_result = claimed
    client.ack_result = completed
    client.status_result = state

    assert delivery.queue_publish("work", "do it") == "msg-q1"
    assert delivery.queue_publish("work", "job", msg_type="job", metadata={"p": 1}) == "msg-q1"
    assert delivery.queue_claim("work") is claimed
    assert delivery.queue_claim("work", lease_seconds=45) is claimed
    assert delivery.queue_ack("msg-q1") is completed
    assert delivery.queue_status("msg-q1") is state
    assert client.queue_calls == [
        ("publish", "work", "do it", "task", None),
        ("publish", "work", "job", "job", {"p": 1}),
        ("claim", "work", 300),
        ("claim", "work", 45),
        ("ack", "msg-q1"),
        ("status", "msg-q1"),
    ]


def test_stream_is_advisory_and_delegated() -> None:
    client, delivery = _delivery()
    callback = lambda message: None  # noqa: E731 - identity matters for the assert

    assert delivery.subscribe("general", callback, include_own=True, coalesce_ms=25) == "sub-1"
    assert client.subscribe_calls == [("general", callback, None, True, 25)]
    assert delivery.recent_warnings == client.warnings


def test_docstring_names_every_entry_point_and_guarantee() -> None:
    doc = AgentDelivery.__doc__ or ""
    for guarantee in ("at-most-once", "at-least-once", "advisory"):
        assert guarantee in doc, f"the delivery contract does not state {guarantee}"
    for name in (
        "pull",
        "read",
        "check_unread",
        "wait",
        "queue_publish",
        "queue_claim",
        "queue_ack",
        "queue_status",
        "subscribe",
        "recent_warnings",
    ):
        assert name in doc, f"{name} is not named in the delivery contract"
