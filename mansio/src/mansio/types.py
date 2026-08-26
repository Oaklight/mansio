"""Core types for mansio message bus."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Message:
    """A message in the bus.

    Attributes:
        id: Unique message identifier (UUID v7, time-ordered).
        channel: Channel name this message belongs to.
        sender: Identifier of the sending agent.
        msg_type: Application-defined message type
            (e.g. "text", "context_sync", "notification", "artifact").
        payload: Message content. JSON string or plain text.
        timestamp: ISO 8601 timestamp of when the message was published.
        metadata: Optional extra fields as a dict.
        parent_id: ID of the message this is a reply to (optional).
        thread_id: ID of the root message of the conversation thread
            (optional). Equals parent_id for direct replies to root;
            enables flat thread queries.
        intent: Application-defined intent label (optional). Suggested
            values: ``REQUIRES_RESPONSE``, ``DIRECT_QUESTION``,
            ``FYI_ONLY``, ``PASS_FLOOR``.  Free-form string; not
            restricted to these values.
    """

    id: str
    channel: str
    sender: str
    msg_type: str
    payload: str
    timestamp: str
    metadata: dict | None = field(default=None)
    parent_id: str | None = field(default=None)
    thread_id: str | None = field(default=None)
    intent: str | None = field(default=None)

    def payload_json(self) -> dict:
        """Parse payload as JSON. Raises ValueError if not valid JSON."""
        return json.loads(self.payload)


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Result of a queue claim or ack operation."""

    message: Message
    status: str
    claimed_by: str
    claimed_at: str
    lease_until: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelMeta:
    """Metadata for a channel.

    Attributes:
        name: Channel name (primary key).
        owner: Agent ID of the channel creator.
        visibility: ``"public"`` or ``"private"``.
        created_at: ISO 8601 timestamp of channel creation.
    """

    name: str
    owner: str
    visibility: str  # "public" | "private"
    created_at: str


@dataclass(frozen=True, slots=True)
class ACLEntry:
    """Access control entry for a channel.

    Attributes:
        channel: Channel name this entry applies to.
        agent_id: Agent granted access.
        permission: Access level — ``"read"``, ``"write"``, or ``"admin"``.
            ``admin`` implies ``write`` implies ``read``.
        granted_at: ISO 8601 timestamp.
        granted_by: Agent that created this entry (optional).
    """

    channel: str
    agent_id: str
    permission: str = field(default="write")  # "read" | "write" | "admin"
    granted_at: str = field(default="")
    granted_by: str | None = field(default=None)


PERMISSION_LEVELS: dict[str, int] = {"read": 0, "write": 1, "admin": 2}
"""Permission hierarchy — admin > write > read."""


@dataclass(frozen=True, slots=True)
class AgentPresence:
    """Presence record for a single agent.

    Attributes:
        agent_id: Unique agent identifier.
        status: ``"online"`` or ``"offline"``.
        last_seen: ISO 8601 timestamp of last heartbeat.
        metadata: Optional extra fields (display_name, capabilities, etc.).
    """

    agent_id: str
    status: str  # "online" | "offline"
    last_seen: str
    metadata: dict | None = field(default=None)
