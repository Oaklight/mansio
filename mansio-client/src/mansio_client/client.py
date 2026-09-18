"""Lightweight MansioClient for remote agent access.

Connects to a mansio server via HTTP/HTTPS. No server-side dependencies
— uses only HttpTransport for communication.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Literal, overload

from mansio_client.transport import HttpTransport
from mansio_client.types import ACLEntry, UserPresence, ClaimResult, Message

if TYPE_CHECKING:
    from collections.abc import Callable

    from mansio_client.injectors import Injector

_USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

_log = logging.getLogger(__name__)


def _tags_match(msg_tags: list[str] | None, filter_tags: list[str]) -> bool:
    if not msg_tags:
        return False
    return all(t in msg_tags for t in filter_tags)


class MansioClient:
    """Stateful agent client for a remote mansio server.

    Args:
        url: Server URL (e.g. ``"https://mansio-api.example.com"``).
        user_id: Unique agent identifier (3-64 chars, lowercase).
        token: Bearer token for API authentication (``mst-...``).
        display_name: Human-readable name. Defaults to user_id.

    Example::

        with MansioClient("https://api.example.com", "my-agent", token="mst-xxx") as client:
            client.channel_send("general", "hello!")
            msgs = client.channel_poll("general")
    """

    def __init__(
        self,
        url: str,
        user_id: str,
        *,
        token: str | None = None,
        display_name: str | None = None,
    ) -> None:
        self._validate_user_id(user_id)
        self._user_id = user_id
        self._display_name = display_name or user_id
        self._transport = HttpTransport(url, user_id=user_id, token=token)
        self._cursors: dict[str, str] = {}
        self._persisted: dict[str, str] = {}

        self._announce()
        self._restore_cursors()

    @staticmethod
    def _validate_user_id(user_id: str) -> None:
        if not _USER_ID_RE.match(user_id):
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
        """Release resources, retrying any cursor write that failed."""
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
        except Exception:
            _log.warning(
                "presence announcement failed for %s", self._user_id, exc_info=True
            )

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
        return self._transport.publish(
            channel,
            self._user_id,
            msg_type,
            content,
            metadata,
            parent_id=parent_id,
            intent=intent,
        )

    def channel_read(
        self,
        channel: str,
        limit: int = 10,
        after: str | None = None,
        order: Literal["oldest", "newest"] = "newest",
        thread_id: str | None = None,
    ) -> list[Message]:
        return self._transport.query(
            channel,
            after=after,
            limit=limit,
            order=order,
            thread_id=thread_id,
        )

    def channel_poll(self, channel: str) -> list[Message]:
        """Return messages on *channel* since this user's last poll.

        The new position is written to the server before returning, so
        it survives a client that never gets to call :meth:`close`.
        """
        cursor = self._cursors.get(channel)
        msgs = self._transport.query(channel, after=cursor)
        if msgs:
            self._cursors[channel] = msgs[-1].id
            self._save_cursors()
        return msgs

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

    def check_unread(self) -> dict[str, int]:
        """Return per-channel unread message counts without advancing cursors.

        Queries each non-system channel for messages newer than the
        last-polled cursor.  Cursors are **not** advanced, so a
        subsequent :meth:`channel_poll` still returns the same messages.

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
        callback: Callable[[Message], None],
        on_error: Callable[[Message, Exception], None] | None = None,
    ) -> str:
        """Subscribe to real-time notifications on a channel.

        Args:
            channel: Channel to subscribe to.
            callback: Called with each new Message.
            on_error: Called with ``(message, exception)`` when *callback*
                raises, so the subscriber can retry or record the failure.
                When omitted, the exception is logged with its traceback.

        Returns:
            Subscription ID for unsubscribe().
        """
        return self._transport.subscribe(channel, callback, on_error=on_error)

    def unsubscribe(self, subscription_id: str) -> None:
        self._transport.unsubscribe(subscription_id)

    def listen(
        self,
        channels: list[str],
        injector: Injector,
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
        return [self.subscribe(ch, injector.inject) for ch in channels]
