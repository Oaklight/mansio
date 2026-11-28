"""Agent-facing delivery over one :class:`MansioClient`."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from mansio_core.types import ClaimResult, Message, WarningEvent

    from mansio_client.client import MansioClient

DEFAULT_WAIT_SECONDS = 30
MAX_WAIT_SECONDS = 120
WAIT_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class WaitResult:
    """The outcome of :meth:`AgentDelivery.wait`."""

    messages: list[Message]
    timed_out: bool


class AgentDelivery:
    """Agent-facing delivery semantics over one :class:`MansioClient`.

    This docstring is the contract: it says which guarantee each entry
    point carries.

    Pull — at-most-once
        :meth:`pull` advances the persisted cursor past everything it
        returns, before returning, so a crash after the call does not
        replay those messages.  :meth:`read` and :meth:`check_unread`
        never advance the cursor; they may return the same messages
        again.  :meth:`wait` is a pull loop on the same cursor, so it is
        at-most-once and blocks until a message arrives or the timeout
        expires.

    Queue — at-least-once
        :meth:`queue_publish`, :meth:`queue_claim`, :meth:`queue_ack`
        and :meth:`queue_status` use leases.  A claim that is not acked
        before its lease expires can be delivered to another consumer,
        so a consumer must be idempotent.

    Stream — advisory
        :meth:`subscribe` pushes messages over SSE.  Delivery is best
        effort: drops and truncation are reported through
        :attr:`recent_warnings`, and a gap is not filled in
        automatically.

    Formatting is not this layer's job.  Callers render ``Message``
    objects for their own surface (MCP tool result, hook text).
    """

    def __init__(self, client: MansioClient) -> None:
        self._client = client

    # ── Pull (at-most-once) ───────────────────────────────────────

    def pull(self, channel: str, *, include_own: bool = False) -> list[Message]:
        """Return messages since the last pull and advance the cursor."""
        return self._client.channel_poll(channel, include_own=include_own)

    def read(
        self,
        channel: str,
        *,
        limit: int = 10,
        after: str | None = None,
        order: Literal["oldest", "newest"] = "newest",
        thread_id: str | None = None,
        intent: str | None = None,
        include_own: bool = False,
    ) -> list[Message]:
        """Read messages without advancing the cursor."""
        return self._client.channel_read(
            channel,
            limit=limit,
            after=after,
            order=order,
            thread_id=thread_id,
            intent=intent,
            include_own=include_own,
        )

    def channels(self, *, detail: bool = False) -> list[str] | list[dict[str, Any]]:
        """List channels without advancing any cursor."""
        return self._client.channel_list(detail=detail)

    def check_unread(self, *, include_own: bool = False) -> dict[str, int]:
        """Count unread messages per channel without advancing cursors."""
        return self._client.check_unread(include_own=include_own)

    def wait(
        self,
        channel: str,
        timeout_seconds: float = DEFAULT_WAIT_SECONDS,
        *,
        include_own: bool = False,
    ) -> WaitResult:
        """Block until a pull returns messages or *timeout_seconds* elapses.

        Raises:
            ValueError: if *timeout_seconds* is not a number, is negative,
                or exceeds :data:`MAX_WAIT_SECONDS`.
        """
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be a number")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if timeout_seconds > MAX_WAIT_SECONDS:
            raise ValueError(f"timeout_seconds must be at most {MAX_WAIT_SECONDS}")

        deadline = time.monotonic() + timeout_seconds
        while True:
            messages = self.pull(channel, include_own=include_own)
            if messages:
                return WaitResult(messages=messages, timed_out=False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return WaitResult(messages=[], timed_out=True)
            time.sleep(min(WAIT_POLL_INTERVAL_SECONDS, remaining))

    # ── Queue (at-least-once) ─────────────────────────────────────

    def queue_publish(
        self,
        channel: str,
        content: str,
        *,
        msg_type: str = "task",
        metadata: dict | None = None,
    ) -> str:
        """Publish a task that can be claimed with a lease."""
        return self._client.queue_publish(channel, content, msg_type=msg_type, metadata=metadata)

    def queue_claim(self, channel: str, *, lease_seconds: int = 300) -> ClaimResult | None:
        """Claim the oldest available task for *lease_seconds*."""
        return self._client.queue_claim(channel, lease_seconds=lease_seconds)

    def queue_ack(self, message_id: str) -> ClaimResult | None:
        """Complete a claimed task."""
        return self._client.queue_ack(message_id)

    def queue_status(self, message_id: str) -> dict | None:
        """Return the claim state of a task, or ``None``."""
        return self._client.queue_status(message_id)

    # ── Stream (advisory) ─────────────────────────────────────────

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None] | Callable[[list[Message]], None],
        *,
        on_error: Callable[[Message, Exception], None] | None = None,
        include_own: bool = False,
        coalesce_ms: int = 0,
    ) -> str:
        """Subscribe over SSE.  Delivery is advisory, not a guarantee."""
        return self._client.subscribe(
            channel,
            callback,
            on_error=on_error,
            include_own=include_own,
            coalesce_ms=coalesce_ms,
        )

    @property
    def recent_warnings(self) -> tuple[WarningEvent, ...]:
        """Recent SSE warnings; advisory and best effort."""
        return self._client.recent_warnings
