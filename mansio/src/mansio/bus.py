"""Message bus implementation."""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, overload

from mansio.admin.metrics import MetricsCollector
from mansio.backends import SQLiteBackend
from mansio.protocols import Backend, ChannelStore, Compactable, Deletable, Presenceable
from mansio.system_policy import CompactionPolicy, system_channel_policy
from mansio.types import ACLEntry, ChannelMeta, ClaimResult, Message, UserPresence

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
        compaction_policy: CompactionPolicy | None = None,
    ) -> None:
        self._backend = backend or SQLiteBackend()
        self._compaction_policy = compaction_policy or system_channel_policy
        self._subs: dict[str, dict[str, Callable[[Message], None]]] = defaultdict(dict)
        self._ensured_channels: set[str] = set()
        self._metrics = MetricsCollector()

    @property
    def backend(self) -> Backend:
        """The underlying message backend."""
        return self._backend

    @property
    def metrics(self) -> MetricsCollector:
        """In-process metrics collector for throughput tracking."""
        return self._metrics

    def get_message(self, message_id: str) -> Message | None:
        """Look up a single message by ID.

        Args:
            message_id: The message ID to look up.

        Returns:
            The Message if found, otherwise None.
        """
        return self._backend.get_message(message_id)

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
        enforce_acl: bool = False,
    ) -> str:
        """Publish a message to a channel.

        Args:
            channel: Target channel name.
            sender: Agent ID of the sender.
            msg_type: Application-defined type string.
            payload: Message content.
            metadata: Optional extra fields.
            parent_id: Optional ID of the message being replied to.
                Server auto-computes ``thread_id`` from the parent.
            intent: Optional intent label for semantic filtering.
            enforce_acl: If True, check write permission before storing.

        Returns:
            The message ID.

        Raises:
            ValueError: If parent_id references a non-existent message
                or a message in a different channel.
            PermissionError: If enforce_acl is True and sender lacks
                write permission.
        """
        # Auto-create channel metadata for sugar prefixes
        if isinstance(self._backend, ChannelStore):
            self._ensure_sugar_channel(channel, sender)

        if enforce_acl and not self.check_access(channel, sender, "write"):
            raise PermissionError(f"agent '{sender}' lacks write permission on '{channel}'")

        msg_id = _uuid7()
        timestamp = _now_iso()

        # Resolve threading fields
        thread_id: str | None = None
        if parent_id is not None:
            parent = self._backend.get_message(parent_id)
            if parent is None:
                raise ValueError(f"parent_id '{parent_id}' not found")
            if parent.channel != channel:
                raise ValueError(
                    f"parent_id '{parent_id}' belongs to channel "
                    f"'{parent.channel}', not '{channel}'"
                )
            # thread_id = parent's thread_id if it's already in a thread,
            # otherwise the parent is the root → use parent's id
            thread_id = parent.thread_id if parent.thread_id else parent_id

        msg = Message(
            id=msg_id,
            channel=channel,
            sender=sender,
            msg_type=msg_type,
            payload=payload,
            timestamp=timestamp,
            metadata=metadata,
            parent_id=parent_id,
            thread_id=thread_id,
            intent=intent,
        )

        if queue:
            self._backend.store_queue(msg)
        else:
            self._backend.store(msg)

        self._metrics.record()
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
        thread_id: str | None = None,
        intent: str | None = None,
        offset: int = 0,
        *,
        user_id: str | None = None,
    ) -> list[Message]:
        """Retrieve messages from a channel.

        Args:
            channel: Channel to read from.
            after: If provided, only return messages with ID greater than this.
            limit: Maximum number of messages to return.
            msg_type: If provided, only return messages of this type.
            order: ``"oldest"`` returns the first *limit* messages;
                ``"newest"`` returns the last *limit* messages.
            thread_id: If provided, only return messages in this thread.
            intent: If provided, only return messages with this intent.
            offset: Number of messages to skip before returning results.
            user_id: If provided, enforce read ACL for this agent.

        Returns:
            Messages in chronological order (oldest first).

        Raises:
            PermissionError: If user_id is provided and the agent
                lacks read permission.
        """
        if user_id is not None and not self.check_access(channel, user_id, "read"):
            raise PermissionError(f"agent '{user_id}' lacks read permission on '{channel}'")
        return self._backend.query(
            channel,
            after=after,
            limit=limit,
            msg_type=msg_type,
            order=order,
            thread_id=thread_id,
            intent=intent,
            offset=offset,
        )

    def message_count(self, channel: str | None = None) -> int:
        """Return the number of stored messages.

        Args:
            channel: If provided, count only messages in this channel.
                Otherwise count all messages across all channels.

        Returns:
            Message count.
        """
        return self._backend.message_count(channel)

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None],
        *,
        user_id: str | None = None,
    ) -> str:
        """Register an in-process callback for new messages on a channel.

        The callback is invoked synchronously during publish() within the
        same process. For cross-process notification, use query() instead.

        Args:
            channel: Channel to watch.
            callback: Function called with each new Message.
            user_id: If provided, enforce read ACL for this agent.

        Returns:
            Subscription ID for use with unsubscribe().

        Raises:
            PermissionError: If user_id is provided and the agent
                lacks read permission.
        """
        if user_id is not None and not self.check_access(channel, user_id, "read"):
            raise PermissionError(f"agent '{user_id}' lacks read permission on '{channel}'")
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
                instead of plain channel name strings. Falls back to
                plain names if the backend doesn't support detail.

        Returns:
            Sorted list of channel names, or list of dicts when
            *detail* is True and supported by the backend.
        """
        if detail:
            try:
                return self._backend.list_channels_detail()
            except NotImplementedError:
                return self._backend.list_channels()
        return self._backend.list_channels()

    def channels_detail(self) -> list[dict]:
        """List all channels with metadata.

        Returns:
            List of dicts with keys: name, message_count, last_activity,
            sender_count, type.

        Raises:
            NotImplementedError: If backend does not implement
                list_channels_detail.
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

    # ── Sugar channel auto-creation ──────────────────────────

    def _ensure_sugar_channel(self, channel: str, sender: str) -> None:
        """Auto-create channel metadata for well-known prefixes.

        - ``dm:alice:bob`` → private, owner = sender, ACL = both agents
        - ``notebook:alice`` → private, owner = alice, ACL = owner-only
        - ``memory:alice`` → private, owner = alice, ACL = owner-only
        - ``broadcast:*`` → public, owner = sender
        - ``_system:*`` → public, owner = ``_system``
        """
        if channel in self._ensured_channels:
            return
        if not isinstance(self._backend, ChannelStore):
            return
        cs = self._backend
        if cs.get_channel(channel) is not None:
            self._ensured_channels.add(channel)
            return

        now = _now_iso()

        if channel.startswith("dm:"):
            parts = channel.split(":")
            agents = sorted(parts[1:])  # canonicalize
            acl = [
                ACLEntry(channel=channel, user_id=a, permission="write", granted_at=now)
                for a in agents
            ]
            meta = ChannelMeta(name=channel, owner=sender, visibility="private", created_at=now)
        elif channel.startswith(("notebook:", "memory:")):
            owner = channel.split(":", 1)[1]
            meta = ChannelMeta(name=channel, owner=owner, visibility="private", created_at=now)
            acl = [ACLEntry(channel=channel, user_id=owner, permission="admin", granted_at=now)]
        elif channel.startswith("broadcast:"):
            meta = ChannelMeta(name=channel, owner=sender, visibility="public", created_at=now)
            acl = None
        elif channel.startswith("_system:"):
            meta = ChannelMeta(name=channel, owner="_system", visibility="public", created_at=now)
            acl = None
        else:
            # Regular user channels — no auto-creation
            return

        with contextlib.suppress(ValueError):
            cs.create_channel(meta, acl)
        self._ensured_channels.add(channel)

    # ── Channel metadata & ACL (optional — ChannelStore backends) ──

    def _require_channel_store(self) -> ChannelStore:
        if not isinstance(self._backend, ChannelStore):
            raise NotImplementedError(
                f"{type(self._backend).__name__} does not implement ChannelStore"
            )
        return self._backend

    def create_channel(
        self,
        name: str,
        owner: str,
        *,
        visibility: str = "public",
        acl: list[ACLEntry] | None = None,
    ) -> ChannelMeta:
        """Create a channel with explicit metadata.

        Args:
            name: Channel name.
            owner: Agent ID of the channel creator.
            visibility: ``"public"`` or ``"private"``.
            acl: Optional initial ACL entries.

        Returns:
            The created ChannelMeta.

        Raises:
            NotImplementedError: If backend is not ChannelStore.
            ValueError: If the channel already exists.
        """
        cs = self._require_channel_store()
        meta = ChannelMeta(name=name, owner=owner, visibility=visibility, created_at=_now_iso())
        cs.create_channel(meta, acl)
        return meta

    def ensure_channel(
        self,
        name: str,
        owner: str,
        *,
        visibility: str = "public",
        acl: list[ACLEntry] | None = None,
    ) -> ChannelMeta:
        """Get or create a channel. Idempotent.

        If the channel already exists, returns existing metadata
        (ignores the owner/visibility/acl arguments).

        Returns:
            The ChannelMeta (existing or newly created).
        """
        cs = self._require_channel_store()
        existing = cs.get_channel(name)
        if existing is not None:
            return existing
        meta = ChannelMeta(name=name, owner=owner, visibility=visibility, created_at=_now_iso())
        try:
            cs.create_channel(meta, acl)
        except ValueError:
            # Race: channel created between get and create
            return cs.get_channel(name) or meta
        return meta

    def get_channel_meta(self, name: str) -> ChannelMeta | None:
        """Return channel metadata, or None."""
        cs = self._require_channel_store()
        return cs.get_channel(name)

    def check_access(self, channel: str, user_id: str, required: str = "read") -> bool:
        """Check if an agent has the required permission on a channel.

        Returns True for backends that don't implement ChannelStore
        (backward compatible: no ACL = open access).
        """
        if not isinstance(self._backend, ChannelStore):
            return True
        return self._backend.check_access(channel, user_id, required)

    def get_acl(self, channel: str) -> list[ACLEntry]:
        """Return ACL entries for a channel."""
        cs = self._require_channel_store()
        return cs.get_acl(channel)

    def set_acl(self, channel: str, entries: list[ACLEntry]) -> None:
        """Replace ACL for a channel."""
        cs = self._require_channel_store()
        cs.set_acl(channel, entries)

    def add_acl_entry(self, entry: ACLEntry) -> None:
        """Add or update a single ACL entry."""
        cs = self._require_channel_store()
        cs.add_acl_entry(entry)

    def remove_acl_entry(self, channel: str, user_id: str) -> bool:
        """Remove an ACL entry."""
        cs = self._require_channel_store()
        return cs.remove_acl_entry(channel, user_id)

    # ── Deletion (optional — Deletable backends only) ────────

    def _require_deletable(self) -> Deletable:
        if not isinstance(self._backend, Deletable):
            raise NotImplementedError(
                f"{type(self._backend).__name__} does not implement Deletable"
            )
        return self._backend

    def delete_channel(self, channel: str, *, user_id: str | None = None) -> int:
        """Delete a channel and all its messages.

        Also removes any in-process subscriptions for the channel.

        Args:
            channel: Channel name to delete.
            user_id: If provided, enforce admin ACL for this agent.

        Returns:
            Number of messages deleted.

        Raises:
            NotImplementedError: If backend is not Deletable.
            PermissionError: If user_id is provided and the agent
                lacks admin permission.
        """
        dl = self._require_deletable()
        if user_id is not None and not self.check_access(channel, user_id, "admin"):
            raise PermissionError(f"agent '{user_id}' lacks admin permission on '{channel}'")
        count = dl.delete_channel(channel)
        # Clean up in-process subscriptions
        self._subs.pop(channel, None)
        # Invalidate sugar-channel cache
        self._ensured_channels.discard(channel)
        # Clean up channel metadata if backend supports it
        if isinstance(self._backend, ChannelStore):
            self._backend.delete_channel_meta(channel)
        return count

    def delete_message(self, message_id: str, *, user_id: str | None = None) -> bool:
        """Delete a single message by ID.

        When *user_id* is provided, the agent must either be the
        message sender (write-level) or have admin permission on
        the message's channel.

        Args:
            message_id: ID of the message to delete.
            user_id: If provided, enforce ownership or admin ACL.

        Returns:
            True if the message was found and deleted.

        Raises:
            NotImplementedError: If backend is not Deletable.
            PermissionError: If user_id is provided and the agent
                is neither the sender nor a channel admin.
        """
        dl = self._require_deletable()
        if user_id is not None:
            msg = self._backend.get_message(message_id)
            if (
                msg is not None
                and msg.sender != user_id
                and not self.check_access(msg.channel, user_id, "admin")
            ):
                raise PermissionError(f"agent '{user_id}' cannot delete message '{message_id}'")
        return dl.delete_message(message_id)

    # ── Presence (optional — Presenceable backends only) ─────

    def _require_presence(self) -> Presenceable:
        if not isinstance(self._backend, Presenceable):
            raise NotImplementedError(
                f"{type(self._backend).__name__} does not implement Presenceable"
            )
        return self._backend

    def heartbeat(self, user_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *user_id*.

        Raises:
            NotImplementedError: If backend is not Presenceable.
        """
        pr = self._require_presence()
        pr.heartbeat(user_id, metadata)

    def users(self, timeout_seconds: int = 120) -> list[UserPresence]:
        """Return all known agents with computed online/offline status.

        Raises:
            NotImplementedError: If backend is not Presenceable.
        """
        pr = self._require_presence()
        return pr.users(timeout_seconds)

    def user_status(self, user_id: str, timeout_seconds: int = 120) -> UserPresence | None:
        """Return presence for a single agent, or ``None`` if unknown.

        Raises:
            NotImplementedError: If backend is not Presenceable.
        """
        pr = self._require_presence()
        return pr.user_status(user_id, timeout_seconds)

    def stats(self) -> dict:
        """Return aggregate statistics for admin dashboard.

        Delegates to the backend's stats() template method.

        Returns:
            Dict with total_messages, total_channels, total_senders,
            channel_breakdown, and msg_type_distribution.
        """
        return self._backend.stats()

    def search(
        self,
        after: str | None = None,
        limit: int = 100,
        channel: str | None = None,
        sender: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Query messages across all channels with optional filters.

        Delegates to the backend's search() template method.

        Args:
            after: Cursor for pagination (message ID).
            limit: Maximum number of messages to return.
            channel: Filter by channel name.
            sender: Filter by sender.
            msg_type: Filter by message type.

        Returns:
            Messages in chronological order (oldest first).
        """
        return self._backend.search(
            after=after, limit=limit, channel=channel, sender=sender, msg_type=msg_type
        )

    def info(self) -> dict:
        """Return backend type and basic info.

        Delegates to the backend's info() template method.

        Returns:
            Dict with at least a 'type' key.
        """
        return self._backend.info()

    # ── Compaction (optional — Compactable backends only) ──────

    def _require_compactable(self) -> Compactable:
        if not isinstance(self._backend, Compactable):
            raise NotImplementedError(
                f"{type(self._backend).__name__} does not implement Compactable"
            )
        return self._backend

    def compact(
        self,
        channel: str,
        *,
        max_messages: int | None = None,
        keep_latest_per_sender: bool = False,
    ) -> int:
        """Compact a channel by removing old messages.

        Args:
            channel: Channel to compact.
            max_messages: If set, keep only the latest *max_messages*
                messages (after per-sender dedup if enabled).
            keep_latest_per_sender: If True, keep only the latest
                message per sender, removing older duplicates.

        Returns:
            Number of messages removed.

        Raises:
            NotImplementedError: If backend is not Compactable.
        """
        cp = self._require_compactable()
        return cp.compact(
            channel,
            max_messages=max_messages,
            keep_latest_per_sender=keep_latest_per_sender,
        )

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

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        force_init: bool = False,
    ) -> None:
        super().__init__(backend=SQLiteBackend(db_path, force_init=force_init))

    def __repr__(self) -> str:
        return f"SQLiteBus({self._backend!r})"
