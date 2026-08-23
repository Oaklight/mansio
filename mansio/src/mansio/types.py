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
    """

    id: str
    channel: str
    sender: str
    msg_type: str
    payload: str
    timestamp: str
    metadata: dict | None = field(default=None)

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
