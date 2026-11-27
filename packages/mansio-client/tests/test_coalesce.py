"""Opt-in batch delivery for SDK subscriptions (#242, item 1)."""

from __future__ import annotations

import mansio_client.coalesce as client_mod
import pytest
from mansio_client import MansioClient
from mansio_client.client import _Coalescer
from mansio_core.types import Message


def _msg(message_id: str) -> Message:
    return Message(
        id=message_id,
        channel="ch",
        sender="other",
        msg_type="chat",
        payload="hi",
        timestamp="2026-01-01T00:00:00Z",
    )


class _FakeTransport:
    def __init__(self) -> None:
        self.subs: dict[str, dict] = {}
        self.unsubscribed: list[str] = []
        self.closed = False
        self._count = 0

    def subscribe(self, channel, callback, on_error=None, *, include_own=True):
        self._count += 1
        sub_id = f"sub-{self._count}"
        self.subs[sub_id] = {
            "channel": channel,
            "callback": callback,
            "on_error": on_error,
            "include_own": include_own,
        }
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        self.unsubscribed.append(sub_id)

    def close(self) -> None:
        self.closed = True


def _client(transport: _FakeTransport) -> MansioClient:
    client = MansioClient.__new__(MansioClient)
    client._transport = transport
    client._coalescers = {}
    return client


@pytest.fixture()
def fake_timer(monkeypatch):
    class FakeTimer:
        instances: list = []

        def __init__(self, interval, function) -> None:
            self.interval = interval
            self.function = function
            self.daemon = False
            self.cancelled = False
            self.started = False
            FakeTimer.instances.append(self)

        def start(self) -> None:
            self.started = True

        def cancel(self) -> None:
            self.cancelled = True

        def fire(self) -> None:
            self.function()

    FakeTimer.instances.clear()
    monkeypatch.setattr(client_mod, "_TIMER_FACTORY", FakeTimer)
    return FakeTimer


class TestCoalescer:
    def test_batches_messages_until_the_timer_fires(self, fake_timer) -> None:
        got: list[list[Message]] = []
        coalescer = _Coalescer(0.05, got.append, None, "ch")

        first = _msg("1")
        second = _msg("2")
        coalescer.add(first)
        coalescer.add(second)
        assert got == []
        assert len(fake_timer.instances) == 1
        assert fake_timer.instances[0].interval == 0.05

        fake_timer.instances[0].fire()
        assert got == [[first, second]]

    def test_flush_starts_a_new_window(self, fake_timer) -> None:
        got: list[list[Message]] = []
        coalescer = _Coalescer(0.05, got.append, None, "ch")

        coalescer.add(_msg("1"))
        fake_timer.instances[0].fire()
        coalescer.add(_msg("2"))
        assert len(fake_timer.instances) == 2
        fake_timer.instances[1].fire()

        assert [m.id for batch in got for m in batch] == ["1", "2"]
        assert len(got) == 2

    def test_close_flushes_the_pending_batch(self, fake_timer) -> None:
        got: list[list[Message]] = []
        coalescer = _Coalescer(0.05, got.append, None, "ch")
        coalescer.add(_msg("1"))

        coalescer.close(flush=True)

        assert [m.id for m in got[0]] == ["1"]
        assert fake_timer.instances[0].cancelled is True

    def test_close_can_drop_the_pending_batch(self, fake_timer) -> None:
        got: list[list[Message]] = []
        coalescer = _Coalescer(0.05, got.append, None, "ch")
        coalescer.add(_msg("1"))

        coalescer.close(flush=False)

        assert got == []

    def test_messages_after_close_are_ignored(self, fake_timer) -> None:
        got: list[list[Message]] = []
        coalescer = _Coalescer(0.05, got.append, None, "ch")
        coalescer.close(flush=True)

        coalescer.add(_msg("1"))

        assert got == []
        assert fake_timer.instances == []

    def test_callback_error_goes_to_on_error(self, fake_timer) -> None:
        errors: list[tuple[Message, Exception]] = []

        def deliver(_batch) -> None:
            raise RuntimeError("boom")

        coalescer = _Coalescer(0.05, deliver, lambda msg, exc: errors.append((msg, exc)), "ch")
        coalescer.add(_msg("1"))
        coalescer.add(_msg("2"))

        fake_timer.instances[0].fire()

        assert errors[0][0].id == "2"
        assert isinstance(errors[0][1], RuntimeError)


class TestClientSubscribe:
    def test_zero_window_passes_the_callback_through(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)

        def callback(_msg: Message) -> None:
            pass

        sub_id = client.subscribe("ch", callback)
        registered = transport.subs[sub_id]
        assert registered["callback"] is callback
        assert registered["channel"] == "ch"
        assert registered["include_own"] is False

    def test_zero_window_forwards_include_own(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)

        sub_id = client.subscribe("ch", lambda _m: None, include_own=True)
        assert transport.subs[sub_id]["include_own"] is True

    def test_coalesced_subscription_delivers_one_batch(self, fake_timer) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        got: list[list[Message]] = []

        sub_id = client.subscribe("ch", got.append, coalesce_ms=50)
        wrapper = transport.subs[sub_id]["callback"]
        first = _msg("1")
        second = _msg("2")
        wrapper(first)
        wrapper(second)

        assert got == []
        assert fake_timer.instances[0].interval == 0.05
        fake_timer.instances[0].fire()
        assert got == [[first, second]]

    def test_negative_window_is_rejected(self) -> None:
        client = _client(_FakeTransport())
        with pytest.raises(ValueError, match="coalesce_ms"):
            client.subscribe("ch", lambda _m: None, coalesce_ms=-1)

    def test_unsubscribe_flushes_the_pending_batch(self, fake_timer) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        got: list[list[Message]] = []

        sub_id = client.subscribe("ch", got.append, coalesce_ms=50)
        transport.subs[sub_id]["callback"](_msg("1"))
        client.unsubscribe(sub_id)

        assert [m.id for m in got[0]] == ["1"]
        assert transport.unsubscribed == [sub_id]

    def test_unsubscribe_unknown_id_still_delegates(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)

        client.unsubscribe("missing")

        assert transport.unsubscribed == ["missing"]

    def test_close_drops_pending_batches(self, fake_timer) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        client._cursors = {}
        client._persisted = {}
        got: list[list[Message]] = []

        sub_id = client.subscribe("ch", got.append, coalesce_ms=50)
        transport.subs[sub_id]["callback"](_msg("1"))

        client.close()

        assert got == []
        assert transport.closed is True
        fake_timer.instances[0].fire()  # a late timer delivers nothing
        assert got == []
