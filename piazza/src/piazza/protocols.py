"""Protocols for piazza pluggable components.

Defines the structural interfaces (PEP 544) that backend, serializer,
and message-bus implementations must satisfy.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from piazza.types import AgentPresence, ClaimResult, Message


class Backend(Protocol):
    """Protocol for message backends.

    A backend handles message transport and persistence as a single
    unit. Implementations can use SQLite, Redis Streams, RabbitMQ,
    MQTT, or pure in-memory storage.
    """

    def store(self, message: Message, *, queue: bool = False) -> None:
        """Persist a message. If queue=True, mark as claimable."""
        ...

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

    def list_channels(self) -> list[str]:
        """List all channels that have at least one message.

        Returns:
            Sorted list of channel names.
        """
        ...

    def close(self) -> None:
        """Release resources held by this backend."""
        ...

    def count_messages(self, channel: str | None = None) -> int:
        """Count messages, optionally filtered by channel.

        Args:
            channel: If provided, count only messages in this channel.

        Returns:
            Number of messages.
        """
        ...

    def query_all(
        self,
        after: str | None = None,
        limit: int = 100,
        channel: str | None = None,
        sender: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Query messages across all channels with optional filters.

        Args:
            after: Cursor for pagination (message ID).
            limit: Maximum number of messages to return.
            channel: Filter by channel name.
            sender: Filter by sender.
            msg_type: Filter by message type.

        Returns:
            Messages in chronological order (oldest first).
        """
        ...

    def get_stats(self) -> dict:
        """Return aggregate statistics for admin dashboard.

        Returns:
            Dict with total_messages, total_channels, total_senders,
            channel_breakdown, and msg_type_distribution.
        """
        ...

    def query_recent_timestamps(self, seconds: int = 60) -> list[str]:
        """Return timestamps of messages from the last N seconds.

        Args:
            seconds: Time window in seconds.

        Returns:
            List of ISO 8601 timestamp strings, sorted ascending.
        """
        ...

    def claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        """Atomically claim the oldest unclaimed/lease-expired message."""
        ...

    def ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Mark a claimed message as completed."""
        ...

    def get_queue_stats(self, channel: str | None = None) -> dict:
        """Return queue status counts (unclaimed, claimed, completed)."""
        ...

    def retire_completed(self, max_age_seconds: int = 86400, max_per_channel: int = 1000) -> int:
        """Remove old completed queue messages. Returns count deleted."""
        ...

    # ── Presence ──────────────────────────────────────────────

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*.

        Upserts ``last_seen`` to now and stores optional *metadata*
        (display_name, capabilities, …).
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


class Serializer(Protocol):
    """Protocol for message payload serialization.

    Handles encoding/decoding of message metadata and payload content.
    Implementations can use JSON, MessagePack, Protobuf, etc.
    """

    def encode(self, obj: dict) -> str:
        """Encode a dict to string representation.

        Args:
            obj: Dictionary to encode.

        Returns:
            Encoded string.
        """
        ...

    def decode(self, data: str) -> dict:
        """Decode a string back to dict.

        Args:
            data: Encoded string.

        Returns:
            Decoded dictionary.
        """
        ...


class MessageBus(Protocol):
    """Protocol for a message bus implementation."""

    def publish(
        self,
        channel: str,
        sender: str,
        msg_type: str,
        payload: str,
        metadata: dict | None = None,
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
        ...

    def poll(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Retrieve messages from a channel.

        Args:
            channel: Channel to read from.
            after: If provided, only return messages with ID greater than this.
                Used as a cursor for incremental polling.
            limit: Maximum number of messages to return.
            msg_type: If provided, only return messages of this type.

        Returns:
            Messages in chronological order (oldest first).
        """
        ...

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None],
    ) -> str:
        """Register an in-process callback for new messages on a channel.

        The callback is invoked synchronously during publish() within the
        same process. For cross-process notification, use poll() instead.

        Args:
            channel: Channel to watch.
            callback: Function called with each new Message.

        Returns:
            Subscription ID for use with unsubscribe().
        """
        ...

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription.

        Args:
            subscription_id: ID returned by subscribe().
        """
        ...

    def channels(self) -> list[str]:
        """List all channels that have at least one message.

        Returns:
            Sorted list of channel names.
        """
        ...
