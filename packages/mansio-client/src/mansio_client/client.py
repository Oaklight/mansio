"""Lightweight MansioClient for remote agent access.

Connects to a mansio server via HTTP/HTTPS. No server-side dependencies
— uses only HttpTransport for communication.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Literal, overload
from uuid import uuid4

from mansio_core.transport import Transport
from mansio_core.types import (
    ACLEntry,
    ChannelMeta,
    ClaimResult,
    Message,
    UserPresence,
    is_valid_user_id,
)

from mansio_client.coalesce import (
    _TIMER_FACTORY as _TIMER_FACTORY,
)
from mansio_client.coalesce import (
    CoalesceMixin,
    _Coalescer,
)
from mansio_client.cursors import CursorMixin
from mansio_client.loop import (
    _LOOP_CLOCK as _LOOP_CLOCK,
)
from mansio_client.loop import (
    LoopMixin,
    LoopSuspected,
    _LoopGuard,
)
from mansio_client.semantic import SemanticMixin
from mansio_client.transport import HttpTransport

_log = logging.getLogger(__name__)


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


class MansioClient(CursorMixin, LoopMixin, CoalesceMixin, SemanticMixin):
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
        self._transport: Transport = HttpTransport(url, user_id=user_id, token=token)
        self._cursors: dict[str, str] = {}
        self._persisted: dict[str, str] = {}
        self._coalescers: dict[str, _Coalescer] = {}
        self._on_loop_suspected = on_loop_suspected
        self._loop_guard = _LoopGuard(user_id) if on_loop_suspected is not None else None

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

    # ── Announce ──────────────────────────────────────────────────

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

    def queue_claim(self, channel: str, *, lease_seconds: int = 300) -> ClaimResult | None:
        return self._transport.queue_claim(channel, self._user_id, lease_seconds=lease_seconds)

    def queue_ack(self, message_id: str) -> ClaimResult | None:
        return self._transport.queue_ack(message_id, self._user_id)

    def queue_status(self, message_id: str) -> dict | None:
        return self._transport.queue_status(message_id)

    # ── Presence ──────────────────────────────────────────────────

    def heartbeat(self, metadata: dict | None = None) -> None:
        self._transport.heartbeat(self._user_id, metadata)

    def users(self, timeout_seconds: int = 120) -> list[UserPresence]:
        return self._transport.users(timeout_seconds)

    def user_status(self, user_id: str, timeout_seconds: int = 120) -> UserPresence | None:
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
