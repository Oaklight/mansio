"""State-machine properties for client cursor persistence and restore.

The enumerated cases in ``packages/mansio-web/tests/test_cursor_persistence.py``
stay as named examples.  This machine runs the real ``MansioClient`` cursor
code (``_restore_cursors`` / ``_save_cursors`` / ``_merge_cursors``) against a
fake server that persists messages and the ``cursor_snapshot`` channel across
client instances, so a restart replays the production path.

Invariants, per channel, at every step:

- delivered ids are exactly the published prefix, in publish order;
- no id is delivered twice;
- the stored cursor is the last delivered id;
- a restart resumes at the same position: no unseen message is skipped and no
  delivered message is handed out again;
- ``channel_read`` never advances the cursor.

Message ids are zero-padded so their string order equals their time order,
which is what ``_merge_cursors`` relies on.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)
from mansio_client import MansioClient
from mansio_client.types import Message

USER_ID = "alice"
CHANNELS = ("alpha", "beta")
SETTINGS = settings(max_examples=25, stateful_step_count=20, deadline=None)


class FakeServer:
    """Persistent message store plus the cursor-snapshot channel."""

    def __init__(self) -> None:
        self.messages: dict[str, list[Message]] = defaultdict(list)
        self.closed = False
        self._next_id = 0

    def _new_id(self) -> str:
        self._next_id += 1
        return f"{self._next_id:06d}"

    def publish(
        self,
        channel: str,
        sender: str,
        msg_type: str,
        payload: str,
        metadata: dict | None = None,
        **_: Any,
    ) -> str:
        mid = self._new_id()
        self.messages[channel].append(
            Message(
                id=mid,
                channel=channel,
                sender=sender,
                msg_type=msg_type,
                payload=payload,
                timestamp=f"t-{mid}",
            )
        )
        return mid

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
        msg_type: str | None = None,
        order: str = "oldest",
        thread_id: str | None = None,
        intent: str | None = None,
        offset: int = 0,
    ) -> list[Message]:
        msgs = list(self.messages.get(channel, []))
        if after is not None:
            ids = [msg.id for msg in msgs]
            assert after in ids, f"unknown cursor {after!r} on {channel!r}"
            msgs = msgs[ids.index(after) + 1 :]
        if msg_type is not None:
            msgs = [msg for msg in msgs if msg.msg_type == msg_type]
        if order == "newest":
            msgs.reverse()
        if offset:
            msgs = msgs[offset:]
        if limit is not None:
            msgs = msgs[:limit]
        return msgs

    def close(self) -> None:
        self.closed = True


def new_client(server: FakeServer) -> MansioClient:
    """Build a client that runs the real cursor code without HTTP."""
    client = MansioClient.__new__(MansioClient)
    client._user_id = USER_ID
    client._display_name = USER_ID
    client._transport = server
    client._cursors = {}
    client._persisted = {}
    client._coalescers = {}
    client._on_loop_suspected = None
    client._loop_guard = None
    client._restore_cursors()
    return client


class CursorMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self._server = FakeServer()
        self._client = new_client(self._server)
        self._published: dict[str, list[str]] = {ch: [] for ch in CHANNELS}
        self._delivered: dict[str, list[str]] = {ch: [] for ch in CHANNELS}
        self._seq = 0

    def teardown(self) -> None:
        self._client.close()

    @rule(channel=st.sampled_from(CHANNELS))
    def publish(self, channel: str) -> None:
        self._seq += 1
        mid = self._server.publish(channel, "bob", "chat", f"m{self._seq}")
        self._published[channel].append(mid)

    @rule(channel=st.sampled_from(CHANNELS))
    def poll(self, channel: str) -> None:
        got = self._client.channel_poll(channel, include_own=True)
        self._delivered[channel].extend(msg.id for msg in got)

    @rule(channel=st.sampled_from(CHANNELS))
    def read(self, channel: str) -> None:
        before = dict(self._client._cursors)
        self._client.channel_read(channel, limit=3)
        assert self._client._cursors == before, "channel_read advanced a cursor"

    @rule()
    def restart(self) -> None:
        self._client.close()
        self._client = new_client(self._server)

    def _latest_snapshot(self) -> dict[str, str] | None:
        channel = self._server.messages.get(f"_system:cursors:{USER_ID}", [])
        for msg in reversed(channel):
            if msg.msg_type == "cursor_snapshot":
                return json.loads(msg.payload)
        return None

    @invariant()
    def cursor_never_skips_or_repeats(self) -> None:
        for channel in CHANNELS:
            delivered = self._delivered[channel]
            published = self._published[channel]
            assert delivered == published[: len(delivered)], channel
            assert len(delivered) == len(set(delivered)), channel
            cursor = self._client._cursors.get(channel)
            if delivered:
                assert cursor == delivered[-1], channel
            else:
                assert cursor is None, channel

        snapshot = self._latest_snapshot()
        if snapshot is None:
            return
        for channel in CHANNELS:
            if self._delivered[channel]:
                assert snapshot.get(channel) == self._delivered[channel][-1], channel


def test_cursor_state_machine() -> None:
    run_state_machine_as_test(CursorMachine, settings=SETTINGS)
