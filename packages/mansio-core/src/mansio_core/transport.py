"""Client-side transport protocol.

``Transport`` is the interface the SDK, CLI and MCP server use to reach a
bus.  It is deliberately protocol-only: ``mansio_core`` defines it, and an
implementation lives outside core (``mansio_client.transport.http`` for the
HTTP/SSE transport).  A ``Bus`` satisfies the in-process subset (publish,
query, subscribe, presence), but not the channel/ACL management calls that
only the HTTP API provides.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol, overload, runtime_checkable

if TYPE_CHECKING:
    from mansio_core.contracts.sse import WarningEvent
    from mansio_core.types import ACLEntry, ChannelMeta, ClaimResult, Message, UserPresence


class Transport(Protocol):
    """Interface for client-to-bus communication."""

    # ── Messages ──────────────────────────────────────────────────

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
    ) -> str:
        """Publish a message. Returns message ID."""
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

    def message_delete(self, message_id: str) -> bool:
        """Delete a message. Returns whether it existed."""
        ...

    # ── Channels ──────────────────────────────────────────────────

    @overload
    def channels(self) -> list[str]: ...
    @overload
    def channels(self, *, detail: Literal[False]) -> list[str]: ...
    @overload
    def channels(self, *, detail: Literal[True]) -> list[dict]: ...

    def channels(self, *, detail: bool = False) -> list[str] | list[dict]:
        """List all channels with messages."""
        ...

    def channel_create(self, name: str, owner: str, visibility: str = "public") -> dict:
        """Create a channel. Returns the channel document."""
        ...

    def channel_meta(self, channel: str) -> ChannelMeta:
        """Return a channel's metadata."""
        ...

    def channel_delete(self, name: str) -> int:
        """Delete a channel. Returns the number of messages removed."""
        ...

    # ── ACL ───────────────────────────────────────────────────────

    def acl_get(self, channel: str) -> list[ACLEntry]:
        """Return the channel's ACL entries."""
        ...

    def acl_set(self, channel: str, entries: list[ACLEntry]) -> int:
        """Replace the channel's ACL. Returns the entry count."""
        ...

    def acl_add(self, channel: str, user_id: str, permission: str = "read") -> ACLEntry:
        """Add one ACL entry."""
        ...

    def acl_remove(self, channel: str, user_id: str) -> bool:
        """Remove one ACL entry. Returns whether it existed."""
        ...

    # ── Queue ─────────────────────────────────────────────────────

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

    # ── Presence ──────────────────────────────────────────────────

    def heartbeat(self, user_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *user_id*."""
        ...

    def users(self, timeout_seconds: int = 120) -> list[UserPresence]:
        """Return all known agents with computed online/offline status."""
        ...

    def user_status(self, user_id: str, timeout_seconds: int = 120) -> UserPresence | None:
        """Return presence for a single agent, or ``None`` if unknown."""
        ...

    # ── Registry ──────────────────────────────────────────────────

    def registry_lookup(self, user_id: str) -> bool:
        """Return whether *user_id* is registered."""
        ...

    # ── Subscriptions ─────────────────────────────────────────────

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None],
        on_error: Callable[[Message, Exception], None] | None = None,
        *,
        include_own: bool = True,
    ) -> str:
        """Subscribe to real-time notifications on a channel.

        Args:
            channel: Channel to subscribe to.
            callback: Called with each new Message.
            on_error: Called with ``(message, exception)`` when *callback*
                raises, so the subscriber can retry or record the failure.
                When omitted, the exception is logged with its traceback.
            include_own: When False, messages sent by this transport's
                ``user_id`` are not delivered to *callback*.

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

    def close(self) -> None:
        """Release resources."""
        ...


@runtime_checkable
class WarningSource(Protocol):
    """A transport that can deliver typed SSE warnings.

    ``Transport`` deliberately does not require warning support. The
    HTTP/SSE transport implements both this protocol and ``Transport``.
    """

    warning_callback: Callable[[WarningEvent], None] | None
