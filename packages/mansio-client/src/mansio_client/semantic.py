"""Convenience methods for :class:`MansioClient`.

Direct messages, notes, thoughts, memory, broadcast, and notification
helpers build on the core send/read/poll API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mansio_core.types import Message

if TYPE_CHECKING:
    from mansio_client._state import ClientState


def _tags_match(msg_tags: list[str] | None, filter_tags: list[str]) -> bool:
    if not msg_tags:
        return False
    return all(t in msg_tags for t in filter_tags)


class SemanticMixin:
    """Sugar for DMs, notes, memory, broadcast, and notifications."""

    @staticmethod
    def _dm_channel(user_a: str, user_b: str) -> str:
        pair = sorted([user_a, user_b])
        return f"dm:{pair[0]}:{pair[1]}"

    def dm_send(self: ClientState, to_user: str, content: str) -> str:
        channel = self._dm_channel(self._user_id, to_user)
        return self.channel_send(channel, content, msg_type="chat")

    def dm_read(self: ClientState, with_user: str, limit: int = 10) -> list[Message]:
        channel = self._dm_channel(self._user_id, with_user)
        return self.channel_read(channel, limit=limit)

    def note_write(self: ClientState, content: str, tags: list[str] | None = None) -> str:
        metadata = {"tags": tags} if tags else None
        return self.channel_send(
            f"notebook:{self._user_id}", content, msg_type="note", metadata=metadata
        )

    def note_read(
        self: ClientState, tags: list[str] | None = None, limit: int = 10
    ) -> list[Message]:
        msgs = self.channel_read(f"notebook:{self._user_id}", limit=limit)
        if tags is None:
            return [m for m in msgs if m.msg_type == "note"]
        return [
            m
            for m in msgs
            if m.msg_type == "note" and m.metadata and _tags_match(m.metadata.get("tags"), tags)
        ]

    def thought_record(
        self: ClientState,
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

    def thought_read(self: ClientState, limit: int = 10) -> list[Message]:
        msgs = self.channel_read(f"notebook:{self._user_id}", limit=limit)
        return [m for m in msgs if m.msg_type == "thought"]

    def memory_store(self: ClientState, content: str, memory_type: str = "general") -> str:
        return self.channel_send(
            f"memory:{self._user_id}",
            content,
            msg_type="memory",
            metadata={"memory_type": memory_type},
        )

    def memory_recall(self: ClientState, query: str, limit: int = 5) -> list[Message]:
        msgs = self.channel_read(f"memory:{self._user_id}", limit=limit * 5)
        memories = [m for m in msgs if m.msg_type == "memory"]
        if query:
            memories = [m for m in memories if query.lower() in m.payload.lower()]
        return memories[:limit]

    def broadcast_list(self: ClientState) -> list[str]:
        return [
            ch.removeprefix("broadcast:")
            for ch in self.channel_list()
            if ch.startswith("broadcast:")
        ]

    def broadcast_read(self: ClientState, topic: str, limit: int = 10) -> list[Message]:
        return self.channel_read(f"broadcast:{topic}", limit=limit)

    def notification_check(self: ClientState) -> list[Message]:
        return self.channel_poll(f"_system:notifications:{self._user_id}")
