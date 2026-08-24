"""Protocols for mansio pluggable components.

Defines the Backend ABC and optional capability protocols
(Presenceable, Compactable).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from mansio._vendor.structlog import get_logger
from mansio.types import AgentPresence, ClaimResult, Message

logger = get_logger(__name__)


class Backend(ABC):
    """Abstract base class for message backends.

    A backend handles message transport and persistence as a single
    unit.  Implementations can use SQLite, Redis Streams, RabbitMQ,
    MQTT, NATS, or pure in-memory storage.

    Abstract methods (must implement):
        store, store_queue, query, list_channels, queue_claim,
        queue_ack, queue_status

    Template methods (have default implementations):
        close, search, message_count, stats, queue_stats,
        queue_retire, recent_timestamps, info
    """

    # === Abstract (must implement) ===

    @abstractmethod
    def store(self, message: Message) -> None:
        """Persist a regular (non-queue) message.

        Args:
            message: Message to store.
        """
        ...

    @abstractmethod
    def store_queue(self, message: Message) -> None:
        """Persist a message and mark it as claimable (queue semantics).

        Args:
            message: Message to store as a queue item.
        """
        ...

    @abstractmethod
    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Retrieve messages from a channel.

        Args:
            channel: Channel to query.
            after: If provided, only return messages with ID > this value.
            limit: Maximum number of messages to return.
            msg_type: If provided, only return messages of this type.

        Returns:
            Messages in chronological order (oldest first).
        """
        ...

    @abstractmethod
    def list_channels(self) -> list[str]:
        """List all channels that have at least one message.

        Returns:
            Sorted list of channel names.
        """
        ...

    @abstractmethod
    def queue_claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        """Atomically claim the oldest unclaimed/lease-expired message."""
        ...

    @abstractmethod
    def queue_ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Mark a claimed message as completed."""
        ...

    @abstractmethod
    def queue_status(self, message_id: str) -> dict | None:
        """Return the queue status dict for a single message.

        Returns:
            Dict with 'status', 'claimed_by', 'claimed_at', etc.,
            or None if the message has no queue status.
        """
        ...

    # === Template methods (have default implementations) ===

    def close(self) -> None:  # noqa: B027
        """Release resources held by this backend."""

    def search(
        self,
        after: str | None = None,
        limit: int = 100,
        channel: str | None = None,
        sender: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Query messages across all channels with optional filters.

        Default implementation iterates list_channels + query.

        Args:
            after: Cursor for pagination (message ID).
            limit: Maximum number of messages to return.
            channel: Filter by channel name.
            sender: Filter by sender.
            msg_type: Filter by message type.

        Returns:
            Messages in chronological order (oldest first).
        """
        if channel:
            msgs = self.query(channel, after=after, limit=limit, msg_type=msg_type)
            if sender:
                msgs = [m for m in msgs if m.sender == sender]
            return msgs
        result: list[Message] = []
        for ch in self.list_channels():
            result.extend(self.query(ch, after=after, limit=10000, msg_type=msg_type))
        if sender:
            result = [m for m in result if m.sender == sender]
        result.sort(key=lambda m: m.id)
        return result[:limit]

    def message_count(self, channel: str | None = None) -> int:
        """Count messages, optionally filtered by channel.

        Default implementation uses search/query.

        Args:
            channel: If provided, count only messages in this channel.

        Returns:
            Number of messages.
        """
        # Naive default — fetches up to 10M messages. Concrete backends should
        # override with efficient implementations (e.g., SQL COUNT/GROUP BY).
        if channel:
            return len(self.query(channel, limit=10_000_000))
        return len(self.search(limit=10_000_000))

    def stats(self) -> dict:
        """Return aggregate statistics for admin dashboard.

        Default implementation scans all messages via search.

        Returns:
            Dict with total_messages, total_channels, total_senders,
            channel_breakdown, and msg_type_distribution.
        """
        all_msgs = self.search(limit=10_000_000)
        channels = self.list_channels()

        senders: set[str] = set()
        types: dict[str, int] = {}
        breakdown: dict[str, dict] = {}

        for m in all_msgs:
            senders.add(m.sender)
            types[m.msg_type] = types.get(m.msg_type, 0) + 1
            if m.channel not in breakdown:
                breakdown[m.channel] = {
                    "channel": m.channel,
                    "message_count": 0,
                    "last_message_time": m.timestamp,
                    "sender_count": 0,
                    "_senders": set(),
                }
            bd = breakdown[m.channel]
            bd["message_count"] += 1
            bd["_senders"].add(m.sender)
            if m.timestamp > bd["last_message_time"]:
                bd["last_message_time"] = m.timestamp

        for bd in breakdown.values():
            bd["sender_count"] = len(bd.pop("_senders"))

        return {
            "total_messages": len(all_msgs),
            "total_channels": len(channels),
            "total_senders": len(senders),
            "channel_breakdown": sorted(
                breakdown.values(),
                key=lambda x: x["message_count"],
                reverse=True,
            ),
            "msg_type_distribution": [
                {"msg_type": t, "count": c}
                for t, c in sorted(types.items(), key=lambda x: x[1], reverse=True)
            ],
        }

    def queue_stats(self, channel: str | None = None) -> dict:
        """Return queue status counts (unclaimed, claimed, completed).

        Default implementation scans messages and checks queue_status.
        """
        result = {"unclaimed": 0, "claimed": 0, "completed": 0}
        channels = [channel] if channel else self.list_channels()
        for ch in channels:
            msgs = self.query(ch, limit=10_000_000)
            for msg in msgs:
                qs = self.queue_status(msg.id)
                if qs and qs.get("status") in result:
                    result[qs["status"]] += 1
        return result

    def queue_retire(self, max_age_seconds: int = 86400, max_per_channel: int = 1000) -> int:
        """Remove old completed queue messages. Returns count deleted.

        Default implementation returns 0 (no-op).
        """
        logger.debug("queue_retire not overridden — no queue cleanup performed")
        return 0

    def recent_timestamps(self, seconds: int = 60) -> list[str]:
        """Return timestamps of messages from the last N seconds.

        Default implementation scans all messages.

        Args:
            seconds: Time window in seconds.

        Returns:
            List of ISO 8601 timestamp strings, sorted ascending.
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        result: list[str] = []
        for ch in self.list_channels():
            for m in self.query(ch, limit=10_000_000):
                if m.timestamp > cutoff:
                    result.append(m.timestamp)
        result.sort()
        return result

    def info(self) -> dict:
        """Return backend type and basic info.

        Returns:
            Dict with at least a 'type' key.
        """
        return {"type": type(self).__name__}


@runtime_checkable
class Presenceable(Protocol):
    """Optional protocol for backends that support agent presence tracking."""

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*.

        Upserts ``last_seen`` to now and stores optional *metadata*
        (display_name, capabilities, ...).
        """
        ...

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        """Return all known agents with computed online/offline status.

        An agent is ``"online"`` if its last heartbeat is within
        *timeout_seconds* of now, otherwise ``"offline"``.
        """
        ...

    def agent_status(self, agent_id: str, timeout_seconds: int = 120) -> AgentPresence | None:
        """Return presence for a single agent, or ``None`` if unknown."""
        ...


@runtime_checkable
class Compactable(Protocol):
    """Optional protocol for backends that support channel compaction."""

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
        """
        ...
