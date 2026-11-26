"""Lightweight MansioClient for remote agent access.

Connects to a mansio server via HTTP/HTTPS. No server-side dependencies
— uses only HttpTransport for communication.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast, overload
from uuid import uuid4

from mansio_client.transport import HttpTransport
from mansio_client.types import (
    ACLEntry,
    ChannelMeta,
    UserPresence,
    ClaimResult,
    Message,
    is_valid_user_id,
)

if TYPE_CHECKING:
    from mansio_client.injectors import Injector

_log = logging.getLogger(__name__)

_TIMER_FACTORY = threading.Timer
"""Timer factory used by coalesced subscriptions; tests inject a fake."""


def _tags_match(msg_tags: list[str] | None, filter_tags: list[str]) -> bool:
    if not msg_tags:
        return False
    return all(t in msg_tags for t in filter_tags)


class _Coalescer:
    """Tumbling-window batch delivery for one subscription.

    The window starts with the first buffered message and flushes once, when
    the timer fires. Messages arriving after a flush start a new window.
    Own-message filtering happens in the transport before this class sees a
    message, so own messages never enter a batch.
    """

    def __init__(
        self,
        window_seconds: float,
        deliver: Callable[[list[Message]], None],
        on_error: Callable[[Message, Exception], None] | None,
        channel: str,
    ) -> None:
        self._window = window_seconds
        self._deliver = deliver
        self._on_error = on_error
        self._channel = channel
        self._lock = threading.Lock()
        self._batch: list[Message] = []
        self._timer: threading.Timer | None = None
        self._closed = False

    def add(self, msg: Message) -> None:
        """Buffer *msg*; start the window timer if this is the first one."""
        with self._lock:
            if self._closed:
                return
            self._batch.append(msg)
            if self._timer is None:
                timer = _TIMER_FACTORY(self._window, self.flush)
                timer.daemon = True
                self._timer = timer
                timer.start()

    def flush(self) -> None:
        """Deliver the buffered batch, if any."""
        with self._lock:
            batch = self._batch
            self._batch = []
            self._timer = None
        if not batch:
            return
        try:
            self._deliver(batch)
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(batch[-1], exc)
            else:
                _log.warning("coalesced callback failed for %s: %s", self._channel, exc)

    def close(self, *, flush: bool) -> None:
        """Stop the timer; deliver the pending batch only if *flush* is true."""
        with self._lock:
            self._closed = True
            timer = self._timer
            self._timer = None
            if timer is not None:
                timer.cancel()
        if flush:
            self.flush()
        else:
            with self._lock:
                self._batch.clear()


class BurstSendError(RuntimeError):
    """A message in a burst failed to send.

    The burst stops at the first failure. Messages before ``failed_index``
    were delivered; their ids are in ``sent_message_ids``, in send order.
    No message at or after ``failed_index`` was attempted. The original
    transport error is available as ``__cause__``.
    """

    def __init__(
        self,
        message: str,
        *,
        burst_id: str,
        sent_message_ids: list[str],
        failed_index: int,
    ) -> None:
        super().__init__(message)
        self.burst_id = burst_id
        self.sent_message_ids = list(sent_message_ids)
        self.failed_index = failed_index


# ── Loop detection (advisory) ─────────────────────────────────────

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
        self._records: deque[tuple[float, str, str | None, str, bool]] = deque(
            maxlen=256
        )
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

    def record(
        self, now: float, channel: str, parent_id: str | None
    ) -> LoopSuspected | None:
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
                r
                for r in self._records
                if r[1] == channel and r[2] == thread_id and r[3] == peer
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


class MansioClient:
    """Stateful agent client for a remote mansio server.

    Args:
        url: Server URL (e.g. ``"https://mansio-api.example.com"``).
        user_id: Unique agent identifier (3-64 chars, lowercase).
        token: Bearer token for API authentication (``mst-...``).
        display_name: Human-readable name. Defaults to user_id.
        on_loop_suspected: Optional advisory callback. When set, the client
            watches its own sends and calls this function once per quiet
            window if it looks like it is exchanging replies with one peer
            in a loop. Detection never drops, delays, or changes a send; the
            callback runs inline after the send is accepted, so keep it
            short. Off by default.

    Example::

        with MansioClient("https://api.example.com", "my-agent", token="mst-xxx") as client:
            client.channel_send("general", "hello!")
            msgs = client.channel_poll("general")
    """

    # Defaults keep loop detection off for instances built without
    # __init__ (tests) and make the send/read hooks harmless when disabled.
    _loop_guard: _LoopGuard | None = None
    _on_loop_suspected: Callable[[LoopSuspected], None] | None = None

    def __init__(
        self,
        url: str,
        user_id: str,
        *,
        token: str | None = None,
        display_name: str | None = None,
        on_loop_suspected: Callable[[LoopSuspected], None] | None = None,
    ) -> None:
        self._validate_user_id(user_id)
        self._user_id = user_id
        self._display_name = display_name or user_id
        self._transport = HttpTransport(url, user_id=user_id, token=token)
        self._cursors: dict[str, str] = {}
        self._persisted: dict[str, str] = {}
        self._coalescers: dict[str, _Coalescer] = {}
        self._on_loop_suspected = on_loop_suspected
        self._loop_guard = (
            _LoopGuard(user_id) if on_loop_suspected is not None else None
        )

        self._announce()
        self._restore_cursors()

    @staticmethod
    def _validate_user_id(user_id: str) -> None:
        if not is_valid_user_id(user_id):
            raise ValueError(
                f"Invalid user_id {user_id!r}: must be 3-64 chars, "
                f"lowercase alphanumeric + hyphens, start/end with alphanumeric."
            )

    # ── Lifecycle ─────────────────────────────────────────────────

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def display_name(self) -> str:
        return self._display_name

    def close(self) -> None:
        """Release resources, retrying any cursor write that failed.

        Pending coalesced batches are dropped: the client is closing, so
        no callback is delivered after ``close()`` returns.
        """
        for coalescer in self._coalescers.values():
            coalescer.close(flush=False)
        self._coalescers.clear()
        self._save_cursors()
        self._transport.close()

    def __enter__(self) -> MansioClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"MansioClient(user_id={self._user_id!r})"

    # ── Announce + Cursors ────────────────────────────────────────

    def _announce(self) -> None:
        """Publish an online presence marker.

        Presence is informational only — no message delivery depends on
        it — so a failure here is logged and the client stays usable.
        """
        try:
            self._transport.publish(
                "_system:agents",
                self._user_id,
                "presence",
                json.dumps({"status": "online"}),
                metadata={"display_name": self._display_name},
            )
        except Exception as exc:
            # A stack trace here would bury the CLI's one-line error when
            # the server is simply unreachable; the message is enough.
            _log.warning("presence announcement failed for %s: %s", self._user_id, exc)

    @property
    def _cursor_channel(self) -> str:
        return f"_system:cursors:{self._user_id}"

    @staticmethod
    def _parse_snapshot(payload: str) -> dict[str, str]:
        """Decode a stored snapshot, ignoring entries of the wrong shape."""
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise TypeError(f"cursor snapshot is {type(data).__name__}, not a mapping")
        return {
            k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)
        }

    def _fetch_cursors(self) -> dict[str, str]:
        """Read the cursor snapshot currently stored on the server.

        The server keeps only the newest snapshot for a user, so a
        single newest-first message is all there is to read.
        """
        msgs = self._transport.query(self._cursor_channel, limit=1, order="newest")
        for msg in msgs:
            if msg.msg_type != "cursor_snapshot":
                continue
            try:
                return self._parse_snapshot(msg.payload)
            except (json.JSONDecodeError, TypeError) as exc:
                # An unreadable snapshot is not recoverable, but refusing to
                # run is worse than restarting from an empty position, so
                # report it and carry on with no stored cursors.
                _log.warning(
                    "ignoring unreadable cursor snapshot for %s: %s",
                    self._user_id,
                    exc,
                )
        return {}

    def _restore_cursors(self) -> None:
        """Load stored cursors at startup.

        Transport failures propagate: a client that starts with no
        position silently replays every message it has already seen,
        which is worse than failing to start.
        """
        self._cursors = self._fetch_cursors()
        self._persisted = dict(self._cursors)

    @staticmethod
    def _merge_cursors(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
        """Combine two cursor maps, keeping the later position per channel.

        Message IDs are time-ordered (UUIDv7), so the larger string is
        the more recent message.
        """
        merged = dict(a)
        for channel, cursor in b.items():
            current = merged.get(channel)
            if current is None or cursor > current:
                merged[channel] = cursor
        return merged

    def _save_cursors(self) -> None:
        """Write advanced cursors to the server, if any have advanced.

        Reads the stored snapshot and merges rather than overwriting it,
        so a session that polls one channel cannot roll back the
        position another session recorded for a different channel.
        """
        if self._cursors == self._persisted:
            return
        try:
            remote = self._fetch_cursors()
            merged = self._merge_cursors(remote, self._cursors)
            if merged != remote:
                self._transport.publish(
                    self._cursor_channel,
                    self._user_id,
                    "cursor_snapshot",
                    json.dumps(merged),
                )
            # Adopt the merged view: another session sharing this user ID
            # may have advanced further than we have, and the user ID is
            # one logical reader.
            self._cursors = merged
            self._persisted = dict(merged)
        except Exception:
            # Leave _persisted untouched so the unsaved advance is
            # retried by the next poll or by close().
            _log.warning(
                "failed to save poll cursors for %s", self._user_id, exc_info=True
            )

    # ── Core API ──────────────────────────────────────────────────

    def channel_send(
        self,
        channel: str,
        content: str,
        msg_type: str = "chat",
        metadata: dict | None = None,
        parent_id: str | None = None,
        intent: str | None = None,
    ) -> str:
        return self._send_message(
            channel,
            content,
            msg_type,
            metadata,
            parent_id,
            intent,
            note_loop=True,
        )

    def _send_message(
        self,
        channel: str,
        content: str,
        msg_type: str,
        metadata: dict | None,
        parent_id: str | None,
        intent: str | None,
        *,
        note_loop: bool,
    ) -> str:
        """Publish one message; *note_loop* feeds the advisory loop guard.

        Bursts pass ``note_loop=False``: their shared ``parent_id`` makes one
        intentional batch look like a run of replies.
        """
        message_id = self._transport.publish(
            channel,
            self._user_id,
            msg_type,
            content,
            metadata,
            parent_id=parent_id,
            intent=intent,
        )
        if note_loop:
            self._note_send(channel, parent_id)
        return message_id

    def _note_send(self, channel: str, parent_id: str | None) -> None:
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

    def _remember_messages(self, msgs: list[Message]) -> None:
        """Cache senders/threads so replies can be attributed to a peer."""
        guard = self._loop_guard
        if guard is None:
            return
        try:
            for msg in msgs:
                guard.observe(msg)
        except Exception:
            _log.debug("loop observation failed", exc_info=True)

    def send_burst(
        self,
        channel: str,
        messages: list[str],
        *,
        msg_type: str = "chat",
        metadata: dict | None = None,
        parent_id: str | None = None,
        intent: str | None = None,
    ) -> list[str]:
        """Send *messages* to *channel* as one deterministic burst.

        Every message carries the same generated ``burst_id`` plus
        ``burst_index`` (0-based) and ``burst_size`` in its free-form
        metadata, merged over *metadata*; the burst fields win on collision.
        Receivers can therefore reassemble the burst without a timing
        window.

        Messages are sent in order, one :meth:`channel_send` request each.
        An empty list sends nothing and returns ``[]``. The burst id is
        generated per call (``uuid4().hex``) and cannot be supplied by the
        caller.

        Args:
            channel: Channel name.
            messages: Payload texts, sent in list order.
            msg_type: Message type for every message.
            metadata: Caller metadata copied onto every message. The dict
                is not mutated.
            parent_id: Parent message id for every message.
            intent: Intent label for every message.

        Returns:
            The server-assigned message ids, in send order.

        Raises:
            BurstSendError: If a send fails. The error carries the ids of
                the messages already sent and the index that failed; later
                messages were not attempted. The original exception is the
                cause. The call cannot be resumed: a retry with
                :meth:`send_burst` starts a new burst with a new id, so the
                caller should treat the remaining texts as a new burst or
                send them individually.
        """
        if not messages:
            return []

        burst_id = uuid4().hex
        size = len(messages)
        sent: list[str] = []
        for index, content in enumerate(messages):
            burst_metadata = {
                **(metadata or {}),
                "burst_id": burst_id,
                "burst_index": index,
                "burst_size": size,
            }
            try:
                sent.append(
                    self._send_message(
                        channel,
                        content,
                        msg_type,
                        burst_metadata,
                        parent_id,
                        intent,
                        note_loop=False,
                    )
                )
            except Exception as exc:
                raise BurstSendError(
                    f"burst {burst_id} failed at message {index + 1} of {size}: {exc}",
                    burst_id=burst_id,
                    sent_message_ids=sent,
                    failed_index=index,
                ) from exc
        return sent

    def channel_read(
        self,
        channel: str,
        limit: int = 10,
        after: str | None = None,
        order: Literal["oldest", "newest"] = "newest",
        thread_id: str | None = None,
        intent: str | None = None,
        include_own: bool = True,
    ) -> list[Message]:
        """Read messages from *channel*.

        Args:
            channel: Channel name.
            limit: Maximum number of messages to return.
            after: Return only messages newer than this message ID.
            order: ``"oldest"`` or ``"newest"`` first.
            thread_id: Restrict to one conversation thread.
            intent: Restrict to messages carrying this intent label.
            include_own: Include messages this user sent (default True;
                the CLI and the MCP read tool default to False).

        Returns:
            Matching messages in the requested order.
        """
        msgs = self._transport.query(
            channel,
            after=after,
            limit=limit,
            order=order,
            thread_id=thread_id,
            intent=intent,
        )
        self._remember_messages(msgs)
        if include_own:
            return msgs
        return [m for m in msgs if m.sender != self._user_id]

    def channel_poll(self, channel: str, *, include_own: bool = False) -> list[Message]:
        """Return messages on *channel* since this user's last poll.

        Messages this user sent are omitted by default; pass
        ``include_own=True`` to receive them. The cursor advances past
        every new message, including the user's own.

        The new position is written to the server before returning, so
        it survives a client that never gets to call :meth:`close`.
        """
        cursor = self._cursors.get(channel)
        msgs = self._transport.query(channel, after=cursor)
        self._remember_messages(msgs)
        if msgs:
            self._cursors[channel] = msgs[-1].id
            self._save_cursors()
        if include_own:
            return msgs
        return [m for m in msgs if m.sender != self._user_id]

    @overload
    def channel_list(self) -> list[str]: ...
    @overload
    def channel_list(self, *, detail: Literal[False]) -> list[str]: ...
    @overload
    def channel_list(self, *, detail: Literal[True]) -> list[dict]: ...

    def channel_list(self, *, detail: bool = False) -> list[str] | list[dict]:
        """List channels on the server.

        Args:
            detail: If True, return list of dicts with channel metadata
                (name, message_count, last_activity, sender_count, type)
                instead of plain channel name strings.

        Returns:
            List of channel name strings, or list of dicts when
            *detail* is True.
        """
        return self._transport.channels(detail=detail)

    # ── DM ────────────────────────────────────────────────────────

    @staticmethod
    def _dm_channel(user_a: str, user_b: str) -> str:
        pair = sorted([user_a, user_b])
        return f"dm:{pair[0]}:{pair[1]}"

    def dm_send(self, to_user: str, content: str) -> str:
        channel = self._dm_channel(self._user_id, to_user)
        return self.channel_send(channel, content, msg_type="chat")

    def dm_read(self, with_user: str, limit: int = 10) -> list[Message]:
        channel = self._dm_channel(self._user_id, with_user)
        return self.channel_read(channel, limit=limit)

    # ── Notes ─────────────────────────────────────────────────────

    def note_write(self, content: str, tags: list[str] | None = None) -> str:
        metadata = {"tags": tags} if tags else None
        return self.channel_send(
            f"notebook:{self._user_id}", content, msg_type="note", metadata=metadata
        )

    def note_read(
        self, tags: list[str] | None = None, limit: int = 10
    ) -> list[Message]:
        msgs = self.channel_read(f"notebook:{self._user_id}", limit=limit)
        if tags is None:
            return [m for m in msgs if m.msg_type == "note"]
        return [
            m
            for m in msgs
            if m.msg_type == "note"
            and m.metadata
            and _tags_match(m.metadata.get("tags"), tags)
        ]

    # ── Thoughts ──────────────────────────────────────────────────

    def thought_record(
        self,
        thought_process: str,
        *,
        thinking_mode: str | None = None,
        focus_area: str | None = None,
    ) -> str:
        metadata: dict = {}
        if thinking_mode:
            metadata["thinking_mode"] = thinking_mode
        if focus_area:
            metadata["focus_area"] = focus_area
        return self.channel_send(
            f"notebook:{self._user_id}",
            thought_process,
            msg_type="thought",
            metadata=metadata or None,
        )

    def thought_read(self, limit: int = 10) -> list[Message]:
        msgs = self.channel_read(f"notebook:{self._user_id}", limit=limit)
        return [m for m in msgs if m.msg_type == "thought"]

    # ── Memory ────────────────────────────────────────────────────

    def memory_store(self, content: str, memory_type: str = "general") -> str:
        return self.channel_send(
            f"memory:{self._user_id}",
            content,
            msg_type="memory",
            metadata={"memory_type": memory_type},
        )

    def memory_recall(self, query: str, limit: int = 5) -> list[Message]:
        msgs = self.channel_read(f"memory:{self._user_id}", limit=limit * 5)
        memories = [m for m in msgs if m.msg_type == "memory"]
        if query:
            memories = [m for m in memories if query.lower() in m.payload.lower()]
        return memories[:limit]

    # ── Broadcast + Notifications ─────────────────────────────────

    def broadcast_list(self) -> list[str]:
        return [
            ch.removeprefix("broadcast:")
            for ch in self.channel_list()
            if ch.startswith("broadcast:")
        ]

    def broadcast_read(self, topic: str, limit: int = 10) -> list[Message]:
        return self.channel_read(f"broadcast:{topic}", limit=limit)

    def notification_check(self) -> list[Message]:
        return self.channel_poll(f"_system:notifications:{self._user_id}")

    def check_unread(self, *, include_own: bool = False) -> dict[str, int]:
        """Return per-channel unread message counts without advancing cursors.

        Queries each non-system channel for messages newer than the
        last-polled cursor.  Cursors are **not** advanced, so a
        subsequent :meth:`channel_poll` still returns the same messages.

        Args:
            include_own: Count messages this user sent (default False,
                matching :meth:`channel_poll`).

        Returns:
            Dict mapping channel name to unread count (only channels
            with at least one unread message are included).
        """
        channels = self.channel_list()
        user_channels = [ch for ch in channels if not ch.startswith("_system:")]
        unread: dict[str, int] = {}
        for ch in user_channels:
            cursor = self._cursors.get(ch)
            msgs = self._transport.query(ch, after=cursor)
            if not include_own:
                msgs = [m for m in msgs if m.sender != self._user_id]
            if msgs:
                unread[ch] = len(msgs)
        return unread

    # ── Queue ─────────────────────────────────────────────────────

    def queue_publish(
        self,
        channel: str,
        content: str,
        msg_type: str = "task",
        metadata: dict | None = None,
    ) -> str:
        return self._transport.publish(
            channel, self._user_id, msg_type, content, metadata, queue=True
        )

    def queue_claim(
        self, channel: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        return self._transport.queue_claim(
            channel, self._user_id, lease_seconds=lease_seconds
        )

    def queue_ack(self, message_id: str) -> ClaimResult | None:
        return self._transport.queue_ack(message_id, self._user_id)

    def queue_status(self, message_id: str) -> dict | None:
        return self._transport.queue_status(message_id)

    # ── Presence ──────────────────────────────────────────────────

    def heartbeat(self, metadata: dict | None = None) -> None:
        self._transport.heartbeat(self._user_id, metadata)

    def users(self, timeout_seconds: int = 120) -> list[UserPresence]:
        return self._transport.users(timeout_seconds)

    def user_status(
        self, user_id: str, timeout_seconds: int = 120
    ) -> UserPresence | None:
        return self._transport.user_status(user_id, timeout_seconds)

    # ── Channel Management ────────────────────────────────────────

    def channel_create(self, name: str, visibility: str = "public") -> dict:
        """Create a channel owned by this user."""
        return self._transport.channel_create(name, self._user_id, visibility)

    def channel_delete(self, name: str) -> int:
        """Delete a channel. Returns number of messages deleted."""
        return self._transport.channel_delete(name)

    def channel_meta(self, channel: str) -> ChannelMeta:
        """Return a channel's metadata (owner, visibility, created_at).

        Raises:
            MansioAPIError: if the channel does not exist or the caller may
                not read it; the server returns 404 for both.
        """
        return self._transport.channel_meta(channel)

    def message_delete(self, message_id: str) -> bool:
        """Delete a single message by ID."""
        return self._transport.message_delete(message_id)

    # ── ACL ───────────────────────────────────────────────────────

    def acl_get(self, channel: str) -> list[ACLEntry]:
        """Return ACL entries for a channel."""
        return self._transport.acl_get(channel)

    def acl_set(self, channel: str, entries: list[ACLEntry]) -> int:
        """Replace all ACL entries for a channel."""
        return self._transport.acl_set(channel, entries)

    def acl_add(self, channel: str, user_id: str, permission: str = "read") -> ACLEntry:
        """Add an ACL entry for a channel."""
        return self._transport.acl_add(channel, user_id, permission)

    def acl_remove(self, channel: str, user_id: str) -> bool:
        """Remove an ACL entry."""
        return self._transport.acl_remove(channel, user_id)

    # ── Registry ──────────────────────────────────────────────────

    def registry_lookup(self, user_id: str) -> bool:
        """Check if a user is registered."""
        return self._transport.registry_lookup(user_id)

    # ── Subscribe / Unsubscribe ───────────────────────────────────

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None] | Callable[[list[Message]], None],
        on_error: Callable[[Message, Exception], None] | None = None,
        *,
        include_own: bool = False,
        coalesce_ms: int = 0,
    ) -> str:
        """Subscribe to real-time notifications on a channel.

        Args:
            channel: Channel to subscribe to.
            callback: Called with each new Message. When *coalesce_ms* is
                positive it is called once per batch with a
                ``list[Message]`` instead.
            on_error: Called with ``(message, exception)`` when *callback*
                raises, so the subscriber can retry or record the failure.
                With coalescing, the message is the last one in the batch.
                When omitted, the exception is logged.
            include_own: Deliver messages this user sent (default False).
                Filtering happens before coalescing, so own messages never
                enter a batch.
            coalesce_ms: Opt-in batch window in milliseconds (default 0 =
                deliver each message immediately). Messages arriving in the
                same window are delivered in one callback.

        Returns:
            Subscription ID for unsubscribe().
        """
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must be >= 0")
        if coalesce_ms == 0:
            return self._transport.subscribe(
                channel,
                cast(Callable[[Message], None], callback),
                on_error=on_error,
                include_own=include_own,
            )
        coalescer = _Coalescer(
            coalesce_ms / 1000.0,
            cast(Callable[[list[Message]], None], callback),
            on_error,
            channel,
        )
        sub_id = self._transport.subscribe(
            channel, coalescer.add, on_error=on_error, include_own=include_own
        )
        self._coalescers[sub_id] = coalescer
        return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Cancel a subscription, delivering any pending coalesced batch first."""
        coalescer = self._coalescers.pop(subscription_id, None)
        if coalescer is not None:
            coalescer.close(flush=True)
        self._transport.unsubscribe(subscription_id)

    def listen(
        self,
        channels: list[str],
        injector: Injector,
        *,
        include_own: bool = False,
    ) -> list[str]:
        """Subscribe to multiple channels, routing messages through an injector.

        Convenience method that subscribes to each channel using the
        injector's :meth:`~Injector.inject` method as the SSE callback.
        Messages arriving on any subscribed channel are delivered to the
        injector in real time.

        Args:
            channels: List of channel names to subscribe to.
            injector: An :class:`~mansio_client.injectors.Injector`
                instance that receives each incoming message.
            include_own: Forward messages this user sent (default False).

        Returns:
            List of subscription IDs (one per channel). Pass these to
            :meth:`unsubscribe` to stop listening.

        The caller is responsible for calling ``injector.close()`` when
        done to release any resources held by the injector.

        Example::

            from mansio_client.injectors import ClaudeCodeInjector

            injector = ClaudeCodeInjector(project_dir=".")
            sub_ids = client.listen(["general", "inbox"], injector)
            # ... later ...
            for sid in sub_ids:
                client.unsubscribe(sid)
        """
        return [
            self.subscribe(ch, injector.inject, include_own=include_own)
            for ch in channels
        ]
