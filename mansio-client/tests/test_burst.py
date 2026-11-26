"""Deterministic burst grouping on the client (issue #242, item 3).

``send_burst`` sends several payloads with one generated ``burst_id`` and
per-message ``burst_index``/``burst_size`` in free-form metadata, so a
receiver does not need a timing window to reassemble them.
"""

from __future__ import annotations

import pytest

from mansio_client import BurstSendError, MansioClient


class _FakeTransport:
    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_at = fail_at

    def publish(
        self,
        channel: str,
        sender: str,
        msg_type: str,
        payload: str,
        metadata: dict | None = None,
        *,
        queue: bool = False,
        parent_id: str | None = None,
        intent: str | None = None,
    ) -> str:
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError("network down")
        self.calls.append(
            {
                "channel": channel,
                "sender": sender,
                "msg_type": msg_type,
                "payload": payload,
                "metadata": metadata,
                "queue": queue,
                "parent_id": parent_id,
                "intent": intent,
            }
        )
        return f"msg-{len(self.calls)}"


def _client(transport: _FakeTransport) -> MansioClient:
    client = MansioClient.__new__(MansioClient)
    client._user_id = "agent-x"
    client._transport = transport
    return client


def _metadata(transport: _FakeTransport, index: int) -> dict:
    return transport.calls[index]["metadata"]


def test_one_burst_id_with_ordered_indexes() -> None:
    transport = _FakeTransport()
    ids = _client(transport).send_burst("general", ["a", "b", "c"])

    assert ids == ["msg-1", "msg-2", "msg-3"]
    assert [c["payload"] for c in transport.calls] == ["a", "b", "c"]
    assert [c["sender"] for c in transport.calls] == ["agent-x", "agent-x", "agent-x"]
    assert [c["channel"] for c in transport.calls] == ["general", "general", "general"]
    assert all(c["queue"] is False for c in transport.calls)

    burst_ids = {_metadata(transport, i)["burst_id"] for i in range(3)}
    assert len(burst_ids) == 1
    burst_id = burst_ids.pop()
    assert len(burst_id) == 32
    assert int(burst_id, 16) >= 0
    assert [_metadata(transport, i)["burst_index"] for i in range(3)] == [0, 1, 2]
    assert [_metadata(transport, i)["burst_size"] for i in range(3)] == [3, 3, 3]


def test_separate_calls_get_distinct_burst_ids() -> None:
    transport = _FakeTransport()
    client = _client(transport)

    client.send_burst("general", ["one"])
    client.send_burst("general", ["two"])

    assert _metadata(transport, 0)["burst_id"] != _metadata(transport, 1)["burst_id"]


def test_caller_metadata_is_preserved_and_burst_fields_win() -> None:
    transport = _FakeTransport()
    metadata = {"trace_id": "t-1", "burst_index": 99, "burst_size": 99}

    _client(transport).send_burst("general", ["a"], metadata=metadata)

    sent = _metadata(transport, 0)
    assert sent["trace_id"] == "t-1"
    assert sent["burst_index"] == 0
    assert sent["burst_size"] == 1
    # The caller's dict is copied, not mutated.
    assert metadata == {"trace_id": "t-1", "burst_index": 99, "burst_size": 99}


def test_empty_list_sends_nothing() -> None:
    transport = _FakeTransport()

    assert _client(transport).send_burst("general", []) == []
    assert transport.calls == []


def test_partial_failure_reports_the_sent_prefix() -> None:
    transport = _FakeTransport(fail_at=2)

    with pytest.raises(BurstSendError) as excinfo:
        _client(transport).send_burst("general", ["a", "b", "c"])

    err = excinfo.value
    assert err.sent_message_ids == ["msg-1", "msg-2"]
    assert err.failed_index == 2
    assert err.burst_id == _metadata(transport, 0)["burst_id"]
    assert isinstance(err.__cause__, RuntimeError)
    assert len(transport.calls) == 2


def test_first_message_failure_reports_no_sent_ids() -> None:
    transport = _FakeTransport(fail_at=0)

    with pytest.raises(BurstSendError) as excinfo:
        _client(transport).send_burst("general", ["a", "b"])

    assert excinfo.value.sent_message_ids == []
    assert excinfo.value.failed_index == 0
    assert transport.calls == []


def test_kwargs_are_forwarded_to_every_message() -> None:
    transport = _FakeTransport()

    _client(transport).send_burst(
        "general",
        ["a", "b"],
        msg_type="notification",
        parent_id="root-1",
        intent="FYI_ONLY",
    )

    for call in transport.calls:
        assert call["msg_type"] == "notification"
        assert call["parent_id"] == "root-1"
        assert call["intent"] == "FYI_ONLY"
