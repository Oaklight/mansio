"""State-machine properties for coalesced subscription delivery.

The enumerated cases in ``test_coalesce.py`` stay as named examples.  This
machine drives the real ``_Coalescer`` with a fake timer (the same
``_TIMER_FACTORY`` seam the enumerated tests use) through arbitrary
add / window-fire / close sequences.

Intended invariants:

- every delivered batch is non-empty;
- flattening the batches reproduces the added ids in order, with no
  duplicates;
- at any time, delivered is a prefix of added; the remainder is either
  pending or, after ``close(flush=False)``, dropped;
- a flush ends the window: the next add arms exactly one new timer;
- ``close(flush=True)`` delivers the pending batch, ``close(flush=False)``
  drops it, and adds after close are ignored.

The documented ``coalesce_ms`` boundary has no message-count cap: 0 means
per-message delivery (no coalescer), negative is rejected, positive is a
tumbling time window.  The non-stateful tests below pin that boundary.
"""

from __future__ import annotations

import mansio_client.client as client_mod
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)
from mansio_client import MansioClient
from mansio_client.client import _Coalescer
from mansio_client.types import Message

CHANNEL = "ch"
SETTINGS = settings(max_examples=25, stateful_step_count=20, deadline=None)


class FakeTimer:
    """Captures the timer instead of scheduling it on a real clock."""

    instances: list[FakeTimer] = []

    def __init__(self, interval: float, function) -> None:
        self.interval = interval
        self.function = function
        self.daemon = False
        self.cancelled = False
        self.fired = False
        FakeTimer.instances.append(self)

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.fired = True
        self.function()


def _message(message_id: str) -> Message:
    return Message(
        id=message_id,
        channel=CHANNEL,
        sender="bob",
        msg_type="chat",
        payload="hi",
        timestamp="2026-01-01T00:00:00Z",
    )


class CoalescerMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        FakeTimer.instances.clear()
        self._batches: list[list[str]] = []
        self._delivered: list[str] = []
        self._added: list[str] = []
        self._pending: list[str] = []
        self._dropped: list[str] = []
        self._closed = False
        self._seq = 0
        self._coalescer = _Coalescer(0.05, self._deliver, None, CHANNEL)

    def _deliver(self, batch: list[Message]) -> None:
        assert batch, "the coalescer delivered an empty batch"
        ids = [msg.id for msg in batch]
        self._batches.append(ids)
        self._delivered.extend(ids)

    @rule()
    def add(self) -> None:
        self._seq += 1
        mid = f"{self._seq:04d}"
        if not self._closed:
            self._added.append(mid)
            self._pending.append(mid)
        self._coalescer.add(_message(mid))

    @rule()
    def fire_window(self) -> None:
        active = [t for t in FakeTimer.instances if not t.cancelled and not t.fired]
        if not active:
            assert not self._pending
            return
        assert self._pending, "a timer is armed with no buffered message"
        assert not self._closed
        expected = list(self._pending)
        active[0].fire()
        assert self._batches[-1] == expected
        self._pending.clear()

    @rule(flush=st.booleans())
    def close(self, flush: bool) -> None:
        if self._closed:
            return
        expected = list(self._pending)
        self._coalescer.close(flush=flush)
        if flush:
            if expected:
                assert self._batches[-1] == expected
        else:
            self._dropped.extend(expected)
        self._pending.clear()
        self._closed = True

    @invariant()
    def order_and_accounting(self) -> None:
        assert len(self._delivered) == len(set(self._delivered))
        assert [mid for batch in self._batches for mid in batch] == self._delivered

        delivered_count = len(self._delivered)
        assert self._added[:delivered_count] == self._delivered
        tail = self._added[delivered_count:]
        if self._closed:
            assert not self._pending
            assert tail == self._dropped
        else:
            assert not self._dropped
            assert tail == self._pending

    @invariant()
    def at_most_one_armed_timer(self) -> None:
        active = [t for t in FakeTimer.instances if not t.cancelled and not t.fired]
        assert len(active) <= 1
        if active:
            assert self._pending and not self._closed
        elif not self._closed:
            assert not self._pending


def test_coalescer_state_machine(monkeypatch) -> None:
    monkeypatch.setattr(client_mod, "_TIMER_FACTORY", FakeTimer)
    run_state_machine_as_test(CoalescerMachine, settings=SETTINGS)


# ── documented boundary, not stateful ───────────────────────────────────


class _FakeTransport:
    def __init__(self) -> None:
        self.subs: dict[str, dict] = {}
        self.unsubscribed: list[str] = []
        self._next = 0

    def subscribe(self, channel, callback, on_error=None, *, include_own=True):
        self._next += 1
        sub_id = f"sub-{self._next}"
        self.subs[sub_id] = {
            "channel": channel,
            "callback": callback,
            "on_error": on_error,
            "include_own": include_own,
        }
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        self.unsubscribed.append(sub_id)


def _bare_client(transport: _FakeTransport) -> MansioClient:
    client = MansioClient.__new__(MansioClient)
    client._transport = transport
    client._coalescers = {}
    return client


def _deliver(transport: _FakeTransport, sub_id: str, message_id: str) -> None:
    transport.subs[sub_id]["callback"](_message(message_id))


def test_coalesce_ms_zero_delivers_per_message() -> None:
    transport = _FakeTransport()
    client = _bare_client(transport)
    callback = lambda msg: None  # noqa: E731

    sub_id = client.subscribe(CHANNEL, callback, coalesce_ms=0)

    assert transport.subs[sub_id]["callback"] is callback
    assert client._coalescers == {}


def test_negative_coalesce_ms_is_rejected() -> None:
    client = _bare_client(_FakeTransport())
    try:
        client.subscribe(CHANNEL, lambda msg: None, coalesce_ms=-1)
    except ValueError as exc:
        assert "coalesce_ms" in str(exc)
    else:
        raise AssertionError("negative coalesce_ms was accepted")


def test_positive_coalesce_ms_batches_and_unsubscribe_flushes(monkeypatch) -> None:
    monkeypatch.setattr(client_mod, "_TIMER_FACTORY", FakeTimer)
    FakeTimer.instances.clear()
    transport = _FakeTransport()
    client = _bare_client(transport)
    got: list[list[Message]] = []

    sub_id = client.subscribe(CHANNEL, got.append, coalesce_ms=10)
    _deliver(transport, sub_id, "0001")
    _deliver(transport, sub_id, "0002")
    assert got == []

    client.unsubscribe(sub_id)

    assert got == [[_message("0001"), _message("0002")]]
    assert transport.unsubscribed == [sub_id]
    assert client._coalescers == {}
