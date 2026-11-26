"""Advisory loop detection on the client (issue #242, item 6).

The client counts its own sends per peer/channel/thread.  When at least
eight sends to one peer happen inside 60 seconds and the last four are
threaded replies to that peer's messages, the opt-in callback fires once
and the guard stays quiet for a window.  Nothing is dropped, delayed, or
changed, and a callback exception never reaches the send path.
"""

from __future__ import annotations

import pytest

import mansio_client.client as client_mod
from mansio_client import LoopSuspected, MansioClient
from mansio_client.client import _LoopGuard
from mansio_client.types import Message


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages: list[Message] = []
        self._count = 0

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
        self._count += 1
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
        return f"msg-{self._count}"

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
        order: str = "oldest",
        thread_id: str | None = None,
        intent: str | None = None,
    ) -> list[Message]:
        return list(self.messages)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(client_mod, "_LOOP_CLOCK", fake)
    return fake


def _client(transport: _FakeTransport, callback=None) -> MansioClient:
    client = MansioClient.__new__(MansioClient)
    client._user_id = "agent-x"
    client._transport = transport
    client._cursors = {}
    client._persisted = {}
    client._coalescers = {}
    client._on_loop_suspected = callback
    client._loop_guard = _LoopGuard("agent-x") if callback is not None else None
    return client


def _msg(message_id: str, sender: str, thread_id: str | None = "root-1") -> Message:
    return Message(
        id=message_id,
        channel="general",
        sender=sender,
        msg_type="chat",
        payload="hi",
        timestamp="2026-01-01T00:00:00Z",
        thread_id=thread_id,
    )


def _seed_peer_messages(
    transport: _FakeTransport, sender: str = "peer", count: int = 8
):
    msgs = [_msg(f"{sender}-{i}", sender) for i in range(count)]
    transport.messages = msgs
    return msgs


def _reply(client: MansioClient, parent_id: str, channel: str = "general") -> str:
    return client.channel_send(channel, "reply", parent_id=parent_id)


class TestLoopGuard:
    def test_alternating_replies_fire_once(self, clock) -> None:
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)
        peers = _seed_peer_messages(transport)
        client.channel_read("general", limit=20)

        for index in range(8):
            _reply(client, peers[index % 2].id)

        assert len(fired) == 1
        hint = fired[0]
        assert hint.channel == "general"
        assert hint.thread_id == "root-1"
        assert hint.peer == "peer"
        assert hint.send_count == 8
        assert hint.window_seconds == 60.0

    def test_slow_sends_do_not_fire(self, clock) -> None:
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)
        peers = _seed_peer_messages(transport)
        client.channel_read("general", limit=20)

        for index in range(8):
            _reply(client, peers[index % 2].id)
            clock.advance(10.0)  # 8 sends spread over 80 s

        assert fired == []

    def test_three_peer_cross_talk_does_not_fire(self, clock) -> None:
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)
        peers = [
            _msg("a-0", "peer-a", thread_id="root-a"),
            _msg("b-0", "peer-b", thread_id="root-b"),
            _msg("c-0", "peer-c", thread_id="root-c"),
        ]
        transport.messages = peers
        client.channel_read("general", limit=20)

        for index in range(9):
            _reply(client, peers[index % 3].id)

        assert fired == []

    def test_below_threshold_does_not_fire(self, clock) -> None:
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)
        peers = _seed_peer_messages(transport)
        client.channel_read("general", limit=20)

        for index in range(7):
            _reply(client, peers[index % 2].id)

        assert fired == []

    def test_quiet_window_stops_repeat_firing(self, clock) -> None:
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)
        peers = _seed_peer_messages(transport)
        client.channel_read("general", limit=20)

        for index in range(8):
            _reply(client, peers[index % 2].id)
        assert len(fired) == 1

        for index in range(8):
            _reply(client, peers[index % 2].id)
        assert len(fired) == 1  # still inside the quiet window

        clock.advance(61.0)
        for index in range(8):
            _reply(client, peers[index % 2].id)
        assert len(fired) == 2

    def test_dm_without_threaded_replies_does_not_fire(self, clock) -> None:
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)

        for _ in range(8):
            client.channel_send("dm:agent-x:peer", "ping")

        assert fired == []

    def test_unknown_parent_is_ignored(self, clock) -> None:
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)

        for index in range(8):
            _reply(client, f"unknown-{index}")

        assert fired == []

    def test_self_thread_does_not_fire(self, clock) -> None:
        """Replying under your own messages is not a two-agent loop."""
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)
        own = [_msg(f"own-{i}", "agent-x") for i in range(8)]
        transport.messages = own
        client.channel_read("general", limit=20)

        for index in range(8):
            _reply(client, own[index % 2].id)

        assert fired == []

    def test_dm_with_self_only_does_not_fire(self, clock) -> None:
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)

        for _ in range(8):
            client.channel_send("dm:agent-x:agent-x", "ping")

        assert fired == []


class TestSendPathUnaffected:
    def test_sends_are_unchanged_in_count_and_order(self, clock) -> None:
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)
        peers = _seed_peer_messages(transport)
        client.channel_read("general", limit=20)

        ids = [_reply(client, peers[index % 2].id) for index in range(8)]

        assert len(fired) == 1
        assert ids == [f"msg-{i}" for i in range(1, 9)]
        assert [c["payload"] for c in transport.calls] == ["reply"] * 8

    def test_callback_exception_does_not_escape(self, clock) -> None:
        transport = _FakeTransport()
        seen: list[LoopSuspected] = []

        def callback(hint: LoopSuspected) -> None:
            seen.append(hint)
            raise RuntimeError("subscriber bug")

        client = _client(transport, callback)
        peers = _seed_peer_messages(transport)
        client.channel_read("general", limit=20)

        ids = [_reply(client, peers[index % 2].id) for index in range(8)]

        assert len(seen) == 1
        assert len(ids) == 8
        assert ids[-1] == "msg-8"

    def test_parented_burst_does_not_fire(self, clock) -> None:
        """One intentional burst of 8 is not a loop; separate sends still are."""
        transport = _FakeTransport()
        fired: list[LoopSuspected] = []
        client = _client(transport, fired.append)
        peers = _seed_peer_messages(transport)
        client.channel_read("general", limit=20)

        client.send_burst("general", ["m"] * 8, parent_id=peers[0].id)
        assert fired == []

        for index in range(8):
            _reply(client, peers[index % 2].id)
        assert len(fired) == 1

    def test_detection_is_off_without_the_callback(self, clock) -> None:
        transport = _FakeTransport()
        client = _client(transport)  # no callback, no guard
        peers = _seed_peer_messages(transport)
        client.channel_read("general", limit=20)

        ids = [_reply(client, peers[index % 2].id) for index in range(8)]

        assert len(ids) == 8
