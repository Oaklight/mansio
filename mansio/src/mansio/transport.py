"""Transport abstraction for MansioClient.

Transport is an internal protocol that decouples MansioClient from
whether the Bus is in-process or behind a network API.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mansio.bus import Bus
    from mansio.types import ClaimResult, Message


class Transport(Protocol):
    """Internal protocol for client-to-bus communication."""

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
        """Publish a message. Returns message ID."""
        ...

    def claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        """Claim the oldest unclaimed/lease-expired message."""
        ...

    def ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Ack a claimed message."""
        ...

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
    ) -> list[Message]:
        """Query messages from a channel."""
        ...

    def list_channels(self) -> list[str]:
        """List all channels with messages."""
        ...

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None],
    ) -> str:
        """Subscribe to real-time notifications on a channel.

        Args:
            channel: Channel to subscribe to.
            callback: Called with each new Message.

        Returns:
            Subscription ID for unsubscribe().
        """
        ...

    def unsubscribe(self, sub_id: str) -> None:
        """Remove a subscription.

        Args:
            sub_id: ID returned by subscribe().
        """
        ...

    @property
    def require_auth(self) -> bool:
        """Whether the underlying bus requires authentication."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...


class LocalTransport:
    """Transport that wraps an in-process Bus object.

    Args:
        bus: The Bus instance to delegate to.
    """

    def __init__(self, bus: Bus) -> None:
        self._bus = bus

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
        """Publish via the local bus."""
        return self._bus.publish(channel, sender, msg_type, payload, metadata, queue=queue)

    def claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        """Claim via the local bus."""
        return self._bus.queue_claim(channel, claimed_by, lease_seconds=lease_seconds)

    def ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Ack via the local bus."""
        return self._bus.queue_ack(message_id, claimed_by)

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
    ) -> list[Message]:
        """Query via the local bus."""
        return self._bus.query(channel, after=after, limit=limit)

    def list_channels(self) -> list[str]:
        """List channels via the local bus."""
        return self._bus.channels()

    @property
    def require_auth(self) -> bool:
        """Delegate to bus.require_auth."""
        return self._bus.require_auth

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None],
    ) -> str:
        """Subscribe via the local bus."""
        return self._bus.subscribe(channel, callback)

    def unsubscribe(self, sub_id: str) -> None:
        """Unsubscribe via the local bus."""
        self._bus.unsubscribe(sub_id)

    def close(self) -> None:
        """No-op -- caller manages bus lifecycle."""

    def __repr__(self) -> str:
        return f"LocalTransport({self._bus!r})"
