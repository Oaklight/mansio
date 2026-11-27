"""Advisory loop detection for :class:`MansioClient`.

The guard counts this client's own sends per peer/channel/thread and flags
sustained reply loops.  Detection is advisory: no send is dropped,
delayed, or changed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mansio_core.types import Message

if TYPE_CHECKING:
    from mansio_client._state import ClientState

_log = logging.getLogger(__name__)

_LOOP_CLOCK = time.monotonic
"""Clock used by the loop guard; tests replace it with a fake."""

_LOOP_WINDOW_SECONDS = 60.0
_LOOP_MIN_SENDS = 8
_LOOP_MIN_REPLIES = 4
_LOOP_QUIET_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class LoopSuspected:
    """Advisory hint passed to ``MansioClient(on_loop_suspected=...)``.

    It is only a hint: no send was dropped, delayed, or changed, and the
    client keeps working if the callback raises.
    """

    channel: str
    thread_id: str | None
    peer: str
    send_count: int
    window_seconds: float


class _LoopGuard:
    """Count this client's own sends and flag sustained reply loops.

    A loop looks like this: the same two agents answer each other, every
    reply is a threaded reply to the peer's previous message, and the rate
    never drops.  The guard fires once when at least ``_LOOP_MIN_SENDS``
    sends to one peer in one channel/thread happened inside the window and
    the last ``_LOOP_MIN_REPLIES`` of them were such replies.  It then stays
    quiet for ``_LOOP_QUIET_SECONDS`` so a loop produces one hint per quiet
    period, not one per send.

    The guard only knows parents that the client has read through
    :meth:`MansioClient.channel_read` or :meth:`MansioClient.channel_poll`.
    An unknown parent, a parent this client sent, or a non-DM channel
    without a peer yields no peer and is not counted: a missed hint is
    better than a wrong one.

    ``observe`` and ``record`` take a lock because subscribe callbacks can
    call :meth:`MansioClient.channel_send` from the SSE thread while
    ``channel_read`` / ``channel_poll`` run on the caller's thread.  The
    advisory callback is invoked by the client outside this lock.
    """

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id
        self._lock = threading.Lock()
        # (timestamp, channel, thread_id, peer, is_threaded_reply)
        self._records: deque[tuple[float, str, str | None, str, bool]] = deque(maxlen=256)
        # message id -> (sender, thread_id) for messages this client has seen
        self._seen: dict[str, tuple[str, str | None]] = {}
        self._quiet_until = 0.0

    def observe(self, msg: Message) -> None:
        """Remember who sent *msg* so a reply can be attributed to a peer."""
        with self._lock:
            self._seen[msg.id] = (msg.sender, msg.thread_id)
            if len(self._seen) > 2048:
                for old in list(self._seen)[:1024]:
                    del self._seen[old]

    def record(self, now: float, channel: str, parent_id: str | None) -> LoopSuspected | None:
        """Record one successful send; return a hint when a loop is suspected."""
        with self._lock:
            peer: str | None = None
            thread_id: str | None = None
            is_reply = False

            if parent_id is not None:
                parent = self._seen.get(parent_id)
                # A thread under the client's own message is not a loop.
                if parent is not None and parent[0] != self._user_id:
                    peer = parent[0]
                    # A root message has no thread_id; its id is the root.
                    thread_id = parent[1] or parent_id
                    is_reply = True

            if peer is None and channel.startswith("dm:"):
                parts = channel.split(":")
                if len(parts) == 3:
                    others = [p for p in parts[1:] if p != self._user_id]
                    if len(others) == 1:
                        peer = others[0]

            if peer is None:
                return None

            self._records.append((now, channel, thread_id, peer, is_reply))
            while self._records and now - self._records[0][0] > _LOOP_WINDOW_SECONDS:
                self._records.popleft()

            group = [
                r for r in self._records if r[1] == channel and r[2] == thread_id and r[3] == peer
            ]
            if len(group) < _LOOP_MIN_SENDS:
                return None
            if not all(r[4] for r in group[-_LOOP_MIN_REPLIES:]):
                return None
            if now < self._quiet_until:
                return None

            self._quiet_until = now + _LOOP_QUIET_SECONDS
            self._records.clear()
            return LoopSuspected(
                channel=channel,
                thread_id=thread_id,
                peer=peer,
                send_count=len(group),
                window_seconds=_LOOP_WINDOW_SECONDS,
            )


class LoopMixin:
    """Client hooks that feed the advisory loop guard."""

    def _note_send(self: ClientState, channel: str, parent_id: str | None) -> None:
        """Feed one successful send to the loop guard; never raises."""
        guard = self._loop_guard
        callback = self._on_loop_suspected
        if guard is None or callback is None:
            return
        try:
            hint = guard.record(_LOOP_CLOCK(), channel, parent_id)
        except Exception:
            _log.debug("loop detection failed", exc_info=True)
            return
        if hint is None:
            return
        try:
            callback(hint)
        except Exception:
            _log.warning("on_loop_suspected callback failed", exc_info=True)

    def _remember_messages(self: ClientState, msgs: list[Message]) -> None:
        """Cache senders/threads so replies can be attributed to a peer."""
        guard = self._loop_guard
        if guard is None:
            return
        try:
            for msg in msgs:
                guard.observe(msg)
        except Exception:
            _log.debug("loop observation failed", exc_info=True)
