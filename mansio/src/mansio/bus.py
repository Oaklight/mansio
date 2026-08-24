"""Message bus implementation."""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, overload

from mansio.backends import SQLiteBackend
from mansio.protocols import Backend, Presenceable
from mansio.system_policy import CompactionPolicy, system_channel_policy
from mansio.types import AgentPresence, ClaimResult, Message

# Thread-safe monotonic sequence for _uuid7 fallback
_seq_lock = threading.Lock()
_seq_last_ms = 0
_seq_counter = 0


def _uuid7() -> str:
    """Generate a UUID v7 (time-ordered) as string.

    Falls back to a time-sortable synthetic ID on Python < 3.14.
    Uses a per-millisecond sequence counter to guarantee strict
    lexicographic ordering even within the same millisecond.
    """
    try:
        return str(uuid.uuid7())
    except AttributeError:
        global _seq_last_ms, _seq_counter  # noqa: PLW0603
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with _seq_lock:
            if ts_ms == _seq_last_ms:
                _seq_counter += 1
            else:
                _seq_last_ms = ts_ms
                _seq_counter = 0
            seq = _seq_counter
        ts_hex = f"{ts_ms:012x}"
        seq_hex = f"{seq:04x}"
        rand = uuid.uuid4().hex[16:]
        return f"{ts_hex[:8]}-{ts_hex[8:12]}-7{seq_hex[:3]}-{seq_hex[3]}{rand[:3]}-{rand[3:15]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Bus:
    """Composable message bus.

    Combines a Backend for message transport/persistence with
    in-process pub/sub.

    Args:
        backend: Message backend for transport and persistence.
            Defaults to in-memory SQLite.
        require_auth: If True, MansioClient must authenticate with
            a registered secret. Defaults to False.
        compaction_policy: Callable invoked after each publish with
            ``(backend, channel)``. Defaults to
            :func:`system_channel_policy`.

    Example:
        >>> bus = Bus()  # in-memory SQLite
        >>> bus = Bus(backend=SQLiteBackend("workspace/.mansio.db"))
        >>> bus = Bus(backend=MemoryBackend())  # pure in-memory for tests
    """

    def __init__(
        self,
        backend: Backend | None = None,
        *,
        require_auth: bool = False,
        compaction_policy: CompactionPolicy | None = None,
    ) -> None:
        self._backend = backend or SQLiteBackend()
        self._require_auth = require_auth
        self._compaction_policy = compaction_policy or system_channel_policy
        self._subs: dict[str, dict[str, Callable[[Message], None]]] = defaultdict(dict)

    @property
    def backend(self) -> Backend:
        """The underlying message backend."""
        return self._backend

    @property
    def require_auth(self) -> bool:
        """Whether this bus requires client authentication."""
        return self._require_auth

    def publish(
        self,
        channel: str,
        sender: str,
        msg_type: str,
        payload: str,
        metadata: dict | None = None,
        *,
        queue: bool = False,
    ) -> str:
        """Publish a message to a channel.

        Args:
            channel: Target channel name.
            sender: Agent ID of the sender.
            msg_type: Application-defined type string.
            payload: Message content.
            metadata: Optional extra fields.

        Returns:
            The message ID.
        """
        msg_id = _uuid7()
        timestamp = _now_iso()

        msg = Message(
            id=msg_id,
            channel=channel,
            sender=sender,
            msg_type=msg_type,
            payload=payload,
            timestamp=timestamp,
            metadata=metadata,
        )

        if queue:
            self._backend.store_queue(msg)
        else:
            self._backend.store(msg)

        self._compaction_policy(self._backend, channel)

        # Notify in-process subscribers (snapshot to avoid mutation during iteration)
        for callback in list(self._subs.get(channel, {}).values()):
            callback(msg)

        return msg_id

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
        msg_type: str | None = None,
        order: Literal["oldest", "newest"] = "oldest",
    ) -> list[Message]:
        """Retrieve messages from a channel.

        Args:
            channel: Channel to read from.
            after: If provided, only return messages with ID greater than this.
            limit: Maximum number of messages to return.
            msg_type: If provided, only return messages of this type.
            order: ``"oldest"`` returns the first *limit* messages;
                ``"newest"`` returns the last *limit* messages.

        Returns:
            Messages in chronological order (oldest first).
        """
        return self._backend.query(
            channel, after=after, limit=limit, msg_type=msg_type, order=order
        )

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None],
    ) -> str:
        """Register an in-process callback for new messages on a channel.

        The callback is invoked synchronously during publish() within the
        same process. For cross-process notification, use query() instead.

        Args:
            channel: Channel to watch.
            callback: Function called with each new Message.

        Returns:
            Subscription ID for use with unsubscribe().
        """
        sub_id = uuid.uuid4().hex[:8]
        self._subs[channel][sub_id] = callback
        return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription.

        Args:
            subscription_id: ID returned by subscribe().
        """
        for channel_subs in self._subs.values():
            channel_subs.pop(subscription_id, None)

    @overload
    def channels(self) -> list[str]: ...
    @overload
    def channels(self, *, detail: Literal[False]) -> list[str]: ...
    @overload
    def channels(self, *, detail: Literal[True]) -> list[dict]: ...

    def channels(self, *, detail: bool = False) -> list[str] | list[dict]:
        """List all channels that have at least one message.

        Args:
            detail: If True, return list of dicts with channel metadata
                instead of plain channel name strings.

        Returns:
            Sorted list of channel names, or list of dicts when
            *detail* is True.
        """
        if detail:
            return self._backend.list_channels_detail()
        return self._backend.list_channels()

    def channels_detail(self) -> list[dict]:
        """List all channels with metadata.

        Returns:
            List of dicts with keys: name, message_count, last_activity,
            sender_count, type.
        """
        return self._backend.list_channels_detail()

    def subscription_counts(self) -> dict[str, list[str]]:
        """Active subscription IDs grouped by channel.

        Only channels with active subscriptions are included.
        """
        return {ch: list(subs.keys()) for ch, subs in self._subs.items() if subs}

    def queue_claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        """Atomically claim the oldest unclaimed/lease-expired message."""
        return self._backend.queue_claim(channel, claimed_by, lease_seconds=lease_seconds)

    def queue_ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Mark a claimed message as completed."""
        return self._backend.queue_ack(message_id, claimed_by)

    def queue_status(self, message_id: str) -> dict | None:
        """Return the queue status dict for a single message.

        Returns:
            Dict with 'status', 'claimed_by', 'claimed_at', etc.,
            or None if the message has no queue status.
        """
        return self._backend.queue_status(message_id)

    # ── Presence (optional — Presenceable backends only) ─────

    def _require_presence(self) -> None:
        if not isinstance(self._backend, Presenceable):
            raise NotImplementedError(
                f"{type(self._backend).__name__} does not implement Presenceable"
            )

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*.

        Raises:
            NotImplementedError: If backend is not Presenceable.
        """
        self._require_presence()
        self._backend.heartbeat(agent_id, metadata)

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        """Return all known agents with computed online/offline status.

        Raises:
            NotImplementedError: If backend is not Presenceable.
        """
        self._require_presence()
        return self._backend.agents(timeout_seconds)

    def agent_status(self, agent_id: str, timeout_seconds: int = 120) -> AgentPresence | None:
        """Return presence for a single agent, or ``None`` if unknown.

        Raises:
            NotImplementedError: If backend is not Presenceable.
        """
        self._require_presence()
        return self._backend.agent_status(agent_id, timeout_seconds)

    def close(self) -> None:
        """Release resources held by the backend."""
        self._backend.close()

    def __enter__(self) -> Bus:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Bus(backend={self._backend!r})"


class SQLiteBus(Bus):
    """Convenience subclass: SQLite-backed bus.

    Shorthand for ``Bus(backend=SQLiteBackend(db_path))``.

    Args:
        db_path: Path to SQLite database file. Use ":memory:" for
            ephemeral in-memory bus (testing).

    Example:
        >>> bus = SQLiteBus("workspace/.mansio.db")
        >>> msg_id = bus.publish("sync", "agent-a", "context_sync", '{"commits": ["abc"]}')
        >>> messages = bus.query("sync")
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        super().__init__(backend=SQLiteBackend(db_path))

    def __repr__(self) -> str:
        return f"SQLiteBus({self._backend!r})"
