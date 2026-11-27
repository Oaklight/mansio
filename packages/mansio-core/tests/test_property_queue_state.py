"""State-machine properties for queue claim/ack/lease semantics.

The enumerated cases in ``packages/mansio-web/tests/test_queue.py`` and the
backend suites stay as named examples.  This machine drives the real
``Bus(MemoryBackend())`` queue through arbitrary publish/claim/ack/advance
sequences and compares every observable state to a small reference model.

A fixed-start fake clock replaces the backend's ``datetime`` so lease expiry
is exercised without sleeping.  Message order is the backend's insertion
order; the reference model claims the oldest claimable message.

Intended invariants (not backend quirks):

- status is one of unclaimed / claimed / completed;
- unclaimed has no holder; claimed and completed name the agent that held it
  (completed keeps the acking agent, matching ``ClaimResult``);
- ack is terminal: a completed message is never claimable again, whatever the
  clock says;
- a claim never hands out a message that another agent still holds under an
  unexpired lease;
- ack by a non-holder returns ``None`` and changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import mansio_core.backends.memory as memory_module
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)
from mansio_core import Bus, MemoryBackend

CHANNEL = "q"
AGENTS = ("alice", "bob")
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
SETTINGS = settings(max_examples=25, stateful_step_count=20, deadline=None)


class FakeClock(datetime):
    """A datetime whose ``now()`` the machine advances explicitly."""

    current = START

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz is not None else cls.current.replace(tzinfo=None)

    @classmethod
    def reset(cls) -> None:
        cls.current = START

    @classmethod
    def advance(cls, seconds: int) -> None:
        cls.current = cls.current + timedelta(seconds=seconds)


@dataclass
class ModelState:
    status: str = "unclaimed"
    holder: str | None = None
    lease_until: datetime | None = None


messages = Bundle("messages")


class QueueMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        FakeClock.reset()
        self._bus = Bus(MemoryBackend())
        self._model: dict[str, ModelState] = {}
        self._order: list[str] = []
        self._acked: set[str] = set()
        self._seq = 0

    def teardown(self) -> None:
        self._bus.close()

    @rule(target=messages)
    def publish(self) -> str:
        self._seq += 1
        mid = self._bus.publish(CHANNEL, "publisher", "task", f"job-{self._seq}", queue=True)
        self._model[mid] = ModelState()
        self._order.append(mid)
        return mid

    @rule(agent=st.sampled_from(AGENTS))
    def claim(self, agent: str) -> None:
        now = FakeClock.now(timezone.utc)
        claimable = [
            mid
            for mid in self._order
            if self._model[mid].status == "unclaimed"
            or (
                self._model[mid].status == "claimed"
                and self._model[mid].lease_until is not None
                and self._model[mid].lease_until < now
            )
        ]
        expected = claimable[0] if claimable else None

        result = self._bus.queue_claim(CHANNEL, agent)
        actual = result.id if result is not None else None
        assert actual == expected, (actual, expected, self._model)

        if result is None:
            return
        assert result.status == "claimed"
        assert result.claimed_by == agent
        assert result.id not in self._acked, "an acked message was claimed again"
        state = self._model[result.id]
        state.status = "claimed"
        state.holder = agent
        state.lease_until = datetime.fromisoformat(result.lease_until)

    @rule(agent=st.sampled_from(AGENTS), mid=messages)
    def ack(self, agent: str, mid: str) -> None:
        state = self._model[mid]
        expected = state.status == "claimed" and state.holder == agent

        result = self._bus.queue_ack(mid, agent)
        assert (result is not None) == expected
        if result is None:
            return
        assert result.status == "completed"
        assert result.claimed_by == agent
        state.status = "completed"
        self._acked.add(mid)

    @rule(seconds=st.integers(min_value=0, max_value=400))
    def advance(self, seconds: int) -> None:
        FakeClock.advance(seconds)

    @invariant()
    def stored_state_matches_model(self) -> None:
        for mid in self._order:
            state = self._model[mid]
            stored = self._bus.queue_status(mid)
            assert stored is not None
            assert stored["status"] == state.status
            if state.status == "unclaimed":
                assert stored["claimed_by"] is None
            else:
                assert stored["claimed_by"] == state.holder
            if mid in self._acked:
                assert stored["status"] == "completed"

    @invariant()
    def completed_stays_completed(self) -> None:
        for mid in self._acked:
            stored = self._bus.queue_status(mid)
            assert stored is not None
            assert stored["status"] == "completed"


def test_queue_state_machine(monkeypatch) -> None:
    monkeypatch.setattr(memory_module, "datetime", FakeClock)
    run_state_machine_as_test(QueueMachine, settings=SETTINGS)
