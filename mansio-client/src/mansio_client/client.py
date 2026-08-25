"""Lightweight MansioClient for remote agent access.

Connects to a mansio server via HTTP/HTTPS. No server-side dependencies
— uses only HttpTransport for communication.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import TYPE_CHECKING, Literal, overload

from mansio_client.transport import HttpTransport
from mansio_client.types import AgentPresence, ClaimResult, Message

if TYPE_CHECKING:
    from collections.abc import Callable

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


def _tags_match(msg_tags: list[str] | None, filter_tags: list[str]) -> bool:
    if not msg_tags:
        return False
    return all(t in msg_tags for t in filter_tags)


class MansioClient:
    """Stateful agent client for a remote mansio server.

    Args:
        url: Server URL (e.g. ``"https://mansio-api.example.com"``).
        agent_id: Unique agent identifier (3-64 chars, lowercase).
        token: Bearer token for API authentication (``mst-...``).
        display_name: Human-readable name. Defaults to agent_id.

    Example::

        with MansioClient("https://api.example.com", "my-agent", token="mst-xxx") as client:
            client.channel_send("general", "hello!")
            msgs = client.channel_poll("general")
    """

    def __init__(
        self,
        url: str,
        agent_id: str,
        *,
        token: str | None = None,
        display_name: str | None = None,
    ) -> None:
        self._validate_agent_id(agent_id)
        self._agent_id = agent_id
        self._display_name = display_name or agent_id
        self._transport = HttpTransport(url, agent_id=agent_id, token=token)
        self._cursors: dict[str, str] = {}

        self._announce()
        self._restore_cursors()

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not _AGENT_ID_RE.match(agent_id):
            raise ValueError(
                f"Invalid agent_id {agent_id!r}: must be 3-64 chars, "
                f"lowercase alphanumeric + hyphens, start/end with alphanumeric."
            )

    # ── Lifecycle ─────────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def display_name(self) -> str:
        return self._display_name

    def close(self) -> None:
        """Save cursors and release resources."""
        self._save_cursors()
        self._transport.close()

    def __enter__(self) -> MansioClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"MansioClient(agent_id={self._agent_id!r})"

    # ── Announce + Cursors ────────────────────────────────────────

    def _announce(self) -> None:
        with contextlib.suppress(Exception):
            self._transport.publish(
                "_system:agents",
                self._agent_id,
                "presence",
                json.dumps({"status": "online"}),
                metadata={"display_name": self._display_name},
            )

    def _restore_cursors(self) -> None:
        cursor_channel = f"_system:cursors:{self._agent_id}"
        with contextlib.suppress(Exception):
            msgs = self._transport.query(cursor_channel, limit=1000)
            for msg in reversed(msgs):
                if msg.msg_type == "cursor_snapshot":
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        self._cursors = json.loads(msg.payload)
                    return

    def _save_cursors(self) -> None:
        if not self._cursors:
            return
        with contextlib.suppress(Exception):
            self._transport.publish(
                f"_system:cursors:{self._agent_id}",
                self._agent_id,
                "cursor_snapshot",
                json.dumps(self._cursors),
            )

    # ── Core API ──────────────────────────────────────────────────

    def channel_send(
        self,
        channel: str,
        content: str,
        msg_type: str = "chat",
        metadata: dict | None = None,
        parent_id: str | None = None,
    ) -> str:
        return self._transport.publish(
            channel,
            self._agent_id,
            msg_type,
            content,
            metadata,
            parent_id=parent_id,
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
        cursor = self._cursors.get(channel)
        msgs = self._transport.query(channel, after=cursor)
        if msgs:
            self._cursors[channel] = msgs[-1].id
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
    def _dm_channel(agent_a: str, agent_b: str) -> str:
        pair = sorted([agent_a, agent_b])
        return f"dm:{pair[0]}:{pair[1]}"

    def dm_send(self, to_agent: str, content: str) -> str:
        channel = self._dm_channel(self._agent_id, to_agent)
        return self.channel_send(channel, content, msg_type="chat")

    def dm_read(self, with_agent: str, limit: int = 10) -> list[Message]:
        channel = self._dm_channel(self._agent_id, with_agent)
        return self.channel_read(channel, limit=limit)

    # ── Notes ─────────────────────────────────────────────────────

    def note_write(self, content: str, tags: list[str] | None = None) -> str:
        metadata = {"tags": tags} if tags else None
        return self.channel_send(
            f"notebook:{self._agent_id}", content, msg_type="note", metadata=metadata
        )

    def note_read(
        self, tags: list[str] | None = None, limit: int = 10
    ) -> list[Message]:
        msgs = self.channel_read(f"notebook:{self._agent_id}", limit=limit)
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
        self, thinking_mode: str, focus_area: str, thought_process: str
    ) -> str:
        return self.channel_send(
            f"notebook:{self._agent_id}",
            thought_process,
            msg_type="thought",
            metadata={"thinking_mode": thinking_mode, "focus_area": focus_area},
        )

    def thought_read(self, limit: int = 10) -> list[Message]:
        msgs = self.channel_read(f"notebook:{self._agent_id}", limit=limit)
        return [m for m in msgs if m.msg_type == "thought"]

    # ── Memory ────────────────────────────────────────────────────

    def memory_store(self, content: str, memory_type: str = "general") -> str:
        return self.channel_send(
            f"memory:{self._agent_id}",
            content,
            msg_type="memory",
            metadata={"memory_type": memory_type},
        )

    def memory_recall(self, query: str, limit: int = 5) -> list[Message]:
        msgs = self.channel_read(f"memory:{self._agent_id}", limit=limit * 5)
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
        return self.channel_poll(f"_system:notifications:{self._agent_id}")

    # ── Queue ─────────────────────────────────────────────────────

    def queue_publish(
        self,
        channel: str,
        content: str,
        msg_type: str = "task",
        metadata: dict | None = None,
    ) -> str:
        return self._transport.publish(
            channel, self._agent_id, msg_type, content, metadata, queue=True
        )

    def queue_claim(
        self, channel: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        return self._transport.queue_claim(
            channel, self._agent_id, lease_seconds=lease_seconds
        )

    def queue_ack(self, message_id: str) -> ClaimResult | None:
        return self._transport.queue_ack(message_id, self._agent_id)

    def queue_status(self, message_id: str) -> dict | None:
        return self._transport.queue_status(message_id)

    # ── Presence ──────────────────────────────────────────────────

    def heartbeat(self, metadata: dict | None = None) -> None:
        self._transport.heartbeat(self._agent_id, metadata)

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        return self._transport.agents(timeout_seconds)

    def agent_status(
        self, agent_id: str, timeout_seconds: int = 120
    ) -> AgentPresence | None:
        return self._transport.agent_status(agent_id, timeout_seconds)

    # ── Subscribe / Unsubscribe ───────────────────────────────────

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None],
    ) -> str:
        return self._transport.subscribe(channel, callback)

    def unsubscribe(self, subscription_id: str) -> None:
        self._transport.unsubscribe(subscription_id)
