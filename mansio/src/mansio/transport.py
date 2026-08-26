"""Transport abstraction for MansioClient.

Transport is an internal protocol that decouples MansioClient from
whether the Bus is in-process or behind a network API.

Bus structurally satisfies this protocol, so no wrapper is needed
for in-process use.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol, overload

if TYPE_CHECKING:
    from mansio.types import AgentPresence, ClaimResult, Message


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

    def queue_claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        """Claim the oldest unclaimed/lease-expired message."""
        ...

    def queue_ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Ack a claimed message."""
        ...

    def queue_status(self, message_id: str) -> dict | None:
        """Return the queue status dict for a single message.

        Returns:
            Dict with 'status', 'claimed_by', 'claimed_at', etc.,
            or None if the message has no queue status.
        """
        ...

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
    ) -> list[Message]:
        """Query messages from a channel."""
        ...

    @overload
    def channels(self) -> list[str]: ...
    @overload
    def channels(self, *, detail: Literal[False]) -> list[str]: ...
    @overload
    def channels(self, *, detail: Literal[True]) -> list[dict]: ...

    def channels(self, *, detail: bool = False) -> list[str] | list[dict]:
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

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription.

        Args:
            subscription_id: ID returned by subscribe().
        """
        ...

    @property
    def require_auth(self) -> bool:
        """Whether the underlying bus requires authentication."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...

    # ── Presence ──────────────────────────────────────────────────

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*."""
        ...

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        """Return all known agents with computed online/offline status."""
        ...

    def agent_status(self, agent_id: str, timeout_seconds: int = 120) -> AgentPresence | None:
        """Return presence for a single agent, or ``None`` if unknown."""
        ...
