"""MansioClient -- stateful agent SDK for mansio message bus.

Provides identity management, cursor tracking, channel naming,
and semantic API (resource_action style) on top of the Bus layer.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from mansio.backends import SQLiteBackend
from mansio.bus import Bus

if TYPE_CHECKING:
    from mansio.transport import Transport
    from mansio.types import ClaimResult, Message

# agent_id regex: lowercase alphanumeric + hyphens, 3-64 chars,
# must start and end with alphanumeric
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


def _tags_match(msg_tags: list[str] | None, filter_tags: list[str]) -> bool:
    """Check if message tags contain all filter tags.

    Args:
        msg_tags: Tags on the message.
        filter_tags: Tags to match against.

    Returns:
        True if all filter_tags are in msg_tags.
    """
    if not msg_tags:
        return False
    return all(t in msg_tags for t in filter_tags)


class MansioClient:
    """Stateful agent client for the mansio message bus.

    Provides identity management, cursor tracking, channel naming
    conventions, and semantic API methods on top of the Bus layer.

    Args:
        target: A Bus object for in-process use, or a string connection
            target (file path, ":memory:", or URL scheme).
        agent_id: Unique agent identifier. Must match pattern
            ``^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$`` (3-64 chars).
        secret: Authentication secret. Required when bus has
            require_auth=True.
        display_name: Human-readable name. Defaults to agent_id.
        token: API token for HttpFrontend authentication (``mst-...``).
            Only used when target is an HTTP URL.

    Example:
        >>> bus = Bus(backend=MemoryBackend())
        >>> client = MansioClient(bus, "test-agent")
        >>> client.note_write("hello world")
        >>> print(client.note_read())
        >>> client.close()

        # Remote with token auth:
        >>> client = MansioClient("http://server:8742", "agent-alice", token="mst-xxx")
    """

    def __init__(
        self,
        target: Bus | str,
        agent_id: str,
        *,
        secret: str | None = None,
        display_name: str | None = None,
        token: str | None = None,
    ) -> None:
        self._validate_agent_id(agent_id)
        self._agent_id = agent_id
        self._display_name = display_name or agent_id
        self._secret = secret
        self._token = token
        self._cursors: dict[str, str] = {}
        self._owns_bus = False
        self._bus: Bus | None = None

        # Resolve target -> transport
        self._transport, self._bus, self._owns_bus = self._resolve_target(target)

        # Authenticate if required
        self._authenticate()

        # Soft registration + cursor restore
        self._announce()
        self._restore_cursors()

    # ── Identity Validation ───────────────────────────────────────

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        """Validate agent_id format.

        Args:
            agent_id: The identifier to validate.

        Raises:
            ValueError: If agent_id doesn't match the required pattern.
        """
        if not _AGENT_ID_RE.match(agent_id):
            raise ValueError(
                f"Invalid agent_id {agent_id!r}: must be 3-64 chars, "
                f"lowercase alphanumeric + hyphens, start/end with alphanumeric."
            )

    # ── Target Resolution ─────────────────────────────────────────

    def _resolve_target(self, target: Bus | str) -> tuple[Transport, Bus | None, bool]:
        """Resolve constructor target to a Transport.

        Args:
            target: Bus object or connection string.

        Returns:
            Tuple of (transport, bus_or_none, owns_bus).

        Raises:
            NotImplementedError: For redis://, amqp://, http://, https://.
            TypeError: For unsupported target types.
        """
        if isinstance(target, Bus):
            return target, target, False

        if not isinstance(target, str):
            raise TypeError(f"target must be Bus or str, got {type(target).__name__}")

        # URL scheme detection
        lower = target.lower()
        if lower.startswith(("http://", "https://")):
            from mansio.transport_http import HttpTransport

            return HttpTransport(target, agent_id=self._agent_id, token=self._token), None, False
        if lower.startswith(("redis://", "amqp://")):
            raise NotImplementedError(
                f"Backend for {lower.split('://')[0]}:// is not yet implemented."
            )

        # File path or :memory: -> auto-create Bus with SQLiteBackend
        db_path: str | Path = target if target == ":memory:" else Path(target)
        bus = Bus(backend=SQLiteBackend(db_path))
        return bus, bus, True

    # ── Authentication ────────────────────────────────────────────

    def _authenticate(self) -> None:
        """Validate secret against registry if bus requires auth.

        Raises:
            PermissionError: If auth required but secret missing/invalid.
        """
        if not self._transport.require_auth:
            return

        if self._secret is None:
            raise PermissionError(
                f"Bus requires authentication but no secret provided "
                f"for agent {self._agent_id!r}. Use MansioClient.register() first."
            )

        # Read registry to find the latest register message for this agent
        registry_msgs = self._transport.query("_system:registry", limit=1000)
        expected_hash = self._find_secret_hash(registry_msgs, self._agent_id)

        if expected_hash is None:
            raise PermissionError(
                f"Agent {self._agent_id!r} is not registered. Use MansioClient.register() first."
            )

        actual_hash = self._hash_secret(self._secret)
        if actual_hash != expected_hash:
            raise PermissionError(f"Invalid secret for agent {self._agent_id!r}.")

    @staticmethod
    def _find_secret_hash(registry_msgs: list[Message], agent_id: str) -> str | None:
        """Find the latest secret hash for an agent from registry messages.

        Args:
            registry_msgs: Messages from _system:registry channel.
            agent_id: Agent to look up.

        Returns:
            The SHA256 hash string, or None if not found.
        """
        for msg in reversed(registry_msgs):
            if msg.sender != agent_id:
                continue
            if msg.msg_type != "register":
                continue
            if msg.metadata and "secret_hash" in msg.metadata:
                action = msg.metadata.get("action", "register")
                if action == "deregister":
                    return None
                return msg.metadata["secret_hash"]
        return None

    @staticmethod
    def _hash_secret(secret: str) -> str:
        """Hash a secret using SHA256.

        Args:
            secret: The plaintext secret.

        Returns:
            String in format "sha256:{hex_digest}".
        """
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    # ── Soft Registration ─────────────────────────────────────────

    def _announce(self) -> None:
        """Write soft-registration presence message to system channels."""
        self._transport.publish(
            "_system:agents",
            self._agent_id,
            "presence",
            json.dumps({"status": "online"}),
            metadata={"display_name": self._display_name},
        )

    # ── Cursor Management ─────────────────────────────────────────

    def _restore_cursors(self) -> None:
        """Restore cursor state from _system:cursors:{agent_id}."""
        cursor_channel = f"_system:cursors:{self._agent_id}"
        msgs = self._transport.query(cursor_channel, limit=1000)
        if not msgs:
            return
        # Take the latest cursor_snapshot
        for msg in reversed(msgs):
            if msg.msg_type == "cursor_snapshot":
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    self._cursors = json.loads(msg.payload)
                return

    def _save_cursors(self) -> None:
        """Persist current cursor state to _system:cursors:{agent_id}."""
        if not self._cursors:
            return
        cursor_channel = f"_system:cursors:{self._agent_id}"
        self._transport.publish(
            cursor_channel,
            self._agent_id,
            "cursor_snapshot",
            json.dumps(self._cursors),
        )

    # ── Registration (class method) ───────────────────────────────

    @classmethod
    def register(
        cls,
        target: Bus | str,
        agent_id: str,
        *,
        display_name: str | None = None,
    ) -> tuple[MansioClient, str]:
        """Register a new agent and return client + generated secret.

        Creates the agent entry in _system:registry with a hashed
        secret, then returns a connected MansioClient and the
        plaintext secret for the caller to store.

        Args:
            target: Bus object or connection string.
            agent_id: Unique agent identifier.
            display_name: Optional human-readable name.

        Returns:
            Tuple of (MansioClient, secret_string).

        Raises:
            ValueError: If agent_id format is invalid.
        """
        cls._validate_agent_id(agent_id)

        # Generate a secret
        secret = f"sk-{secrets.token_hex(24)}"
        secret_hash = cls._hash_secret(secret)

        # We need to write the registration before constructing normally,
        # because __init__ runs _authenticate() which would fail.
        # Use object.__new__ to bypass __init__, set up transport manually,
        # write registration, then finalize initialization.
        inst = object.__new__(cls)
        inst._agent_id = agent_id
        inst._display_name = display_name or agent_id
        inst._secret = secret
        inst._cursors = {}
        inst._bus = None
        inst._owns_bus = False
        inst._transport, inst._bus, inst._owns_bus = inst._resolve_target(target)

        # Write registration message BEFORE auth can be checked
        inst._transport.publish(
            "_system:registry",
            agent_id,
            "register",
            json.dumps(
                {
                    "agent_id": agent_id,
                    "display_name": display_name or agent_id,
                }
            ),
            metadata={
                "display_name": display_name or agent_id,
                "secret_hash": secret_hash,
                "action": "register",
            },
        )

        # Now announce and restore cursors (normal init steps minus auth)
        inst._announce()
        inst._restore_cursors()

        return inst, secret

    # ── Properties ────────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        """The unique agent identifier."""
        return self._agent_id

    @property
    def display_name(self) -> str:
        """The display name for this agent."""
        return self._display_name

    @property
    def transport(self) -> Transport:
        """The underlying transport (internal use)."""
        return self._transport

    # ── Lifecycle ─────────────────────────────────────────────────

    def close(self) -> None:
        """Save cursors and release resources.

        If this client created the bus (from a connection string),
        the bus is also closed.
        """
        self._save_cursors()
        if self._owns_bus and self._bus is not None:
            self._bus.close()

    def __enter__(self) -> MansioClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"MansioClient(agent_id={self._agent_id!r}, transport={self._transport!r})"

    # ── Core API: Channel Operations ──────────────────────────────

    def channel_send(
        self,
        channel: str,
        content: str,
        msg_type: str = "chat",
        metadata: dict | None = None,
    ) -> str:
        """Send a message to a channel.

        Args:
            channel: Target channel name.
            content: Message content (text or JSON string).
            msg_type: Message type. Defaults to "chat".
            metadata: Optional extra fields.

        Returns:
            The message ID.
        """
        return self._transport.publish(channel, self._agent_id, msg_type, content, metadata)

    def channel_read(
        self,
        channel: str,
        limit: int = 10,
        after: str | None = None,
    ) -> list[Message]:
        """Read messages from a channel (does NOT advance cursor).

        Args:
            channel: Channel to read.
            limit: Maximum messages to return.
            after: Return only messages after this ID.

        Returns:
            Messages in chronological order.
        """
        return self._transport.query(channel, after=after, limit=limit)

    def channel_poll(self, channel: str) -> list[Message]:
        """Poll for new messages, auto-advancing the cursor.

        Args:
            channel: Channel to poll.

        Returns:
            New messages since last poll.
        """
        cursor = self._cursors.get(channel)
        msgs = self._transport.query(channel, after=cursor)
        if msgs:
            self._cursors[channel] = msgs[-1].id
        return msgs

    def channel_list(self) -> list[str]:
        """List all channels with messages.

        Returns:
            Sorted list of channel names.
        """
        return self._transport.channels()

    # ── Sugar API: Notes ──────────────────────────────────────────

    def note_write(
        self,
        content: str,
        tags: list[str] | None = None,
    ) -> str:
        """Write a note to the agent's notebook.

        Args:
            content: Note content.
            tags: Optional tags for categorization.

        Returns:
            The message ID.
        """
        metadata = {"tags": tags} if tags else None
        return self.channel_send(
            f"notebook:{self._agent_id}", content, msg_type="note", metadata=metadata
        )

    def note_read(
        self,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[Message]:
        """Read notes from the agent's notebook.

        Args:
            tags: If provided, filter to notes with matching tags.
            limit: Maximum messages to fetch from channel.

        Returns:
            Notes in chronological order.
        """
        msgs = self.channel_read(f"notebook:{self._agent_id}", limit=limit)
        if tags is None:
            return [m for m in msgs if m.msg_type == "note"]
        return [
            m
            for m in msgs
            if m.msg_type == "note" and m.metadata and _tags_match(m.metadata.get("tags"), tags)
        ]

    # ── Sugar API: Thoughts ───────────────────────────────────────

    def thought_record(
        self,
        thinking_mode: str,
        focus_area: str,
        thought_process: str,
    ) -> str:
        """Record a cognitive process to the agent's notebook.

        Args:
            thinking_mode: Type of thinking (e.g. "reasoning", "planning",
                "reflection", "recalling", "brainstorming", "exploring").
            focus_area: What topic is being thought about.
            thought_process: The actual thought content.

        Returns:
            The message ID.
        """
        return self.channel_send(
            f"notebook:{self._agent_id}",
            thought_process,
            msg_type="thought",
            metadata={"thinking_mode": thinking_mode, "focus_area": focus_area},
        )

    def thought_read(self, limit: int = 10) -> list[Message]:
        """Read thought records from the agent's notebook.

        Args:
            limit: Maximum messages to fetch from channel.

        Returns:
            Thought messages in chronological order.
        """
        msgs = self.channel_read(f"notebook:{self._agent_id}", limit=limit)
        return [m for m in msgs if m.msg_type == "thought"]

    # ── Sugar API: Memory ─────────────────────────────────────────

    def memory_store(
        self,
        content: str,
        memory_type: str = "general",
    ) -> str:
        """Store a memory entry.

        Args:
            content: Memory content.
            memory_type: Type of memory (e.g. "general", "fact",
                "experience", "decision", "preference").

        Returns:
            The message ID.
        """
        return self.channel_send(
            f"memory:{self._agent_id}",
            content,
            msg_type="memory",
            metadata={"memory_type": memory_type},
        )

    def memory_recall(
        self,
        query: str,
        limit: int = 5,
    ) -> list[Message]:
        """Recall memories matching a query (default: substring match).

        The default implementation uses substring matching on payload.
        Semantic search capability can be added via external integrations.

        Args:
            query: Search query (used for substring matching).
            limit: Maximum memories to return.

        Returns:
            Matching memory messages.
        """
        # Fetch more than limit to filter, then trim
        msgs = self.channel_read(f"memory:{self._agent_id}", limit=limit * 5)
        memories = [m for m in msgs if m.msg_type == "memory"]
        if query:
            memories = [m for m in memories if query.lower() in m.payload.lower()]
        return memories[:limit]

    # ── Sugar API: DM ─────────────────────────────────────────────

    @staticmethod
    def _dm_channel(agent_a: str, agent_b: str) -> str:
        """Compute the canonical DM channel name.

        Agent IDs are sorted lexicographically to ensure both sides
        use the same channel name.

        Args:
            agent_a: First agent ID.
            agent_b: Second agent ID.

        Returns:
            Channel name in format "dm:{lower}:{higher}".
        """
        pair = sorted([agent_a, agent_b])
        return f"dm:{pair[0]}:{pair[1]}"

    def dm_send(self, to_agent: str, content: str) -> str:
        """Send a direct message to another agent.

        If the target agent has never registered (no heartbeat on
        record), a :class:`UserWarning` is emitted.  The message is
        still delivered so that pre-provisioned agents can receive it
        once they come online.

        Args:
            to_agent: Recipient agent ID.
            content: Message content.

        Returns:
            The message ID.
        """
        # Best-effort warning when the recipient has never been seen.
        # Failures (backend lacks presence, server error, etc.) are
        # silently ignored — a presence check must never block sending.
        try:
            if self._transport.agent_status(to_agent) is None:
                warnings.warn(
                    f"target agent '{to_agent}' is not registered; "
                    f"message stored but may never be read",
                    UserWarning,
                    stacklevel=2,
                )
        except Exception:  # noqa: BLE001
            pass
        channel = self._dm_channel(self._agent_id, to_agent)
        return self.channel_send(channel, content, msg_type="chat")

    def dm_read(
        self,
        with_agent: str,
        limit: int = 10,
    ) -> list[Message]:
        """Read direct messages with another agent.

        Args:
            with_agent: The other agent's ID.
            limit: Maximum messages to return.

        Returns:
            Messages in chronological order.
        """
        channel = self._dm_channel(self._agent_id, with_agent)
        return self.channel_read(channel, limit=limit)

    # ── Sugar API: Broadcast ──────────────────────────────────────

    def broadcast_list(self) -> list[str]:
        """List all broadcast topics.

        Returns:
            List of topic names (without the "broadcast:" prefix).
        """
        return [
            ch.removeprefix("broadcast:")
            for ch in self.channel_list()
            if ch.startswith("broadcast:")
        ]

    def broadcast_read(
        self,
        topic: str,
        limit: int = 10,
    ) -> list[Message]:
        """Read messages from a broadcast topic.

        Args:
            topic: Broadcast topic name.
            limit: Maximum messages to return.

        Returns:
            Messages in chronological order.
        """
        return self.channel_read(f"broadcast:{topic}", limit=limit)

    # ── Sugar API: Queue ──────────────────────────────────────────

    def queue_publish(
        self,
        channel: str,
        content: str,
        msg_type: str = "task",
        metadata: dict | None = None,
    ) -> str:
        """Publish a claimable task to a channel.

        Args:
            channel: Target channel.
            content: Task payload.
            msg_type: Message type. Defaults to "task".
            metadata: Optional extra fields.

        Returns:
            The message ID.
        """
        return self._transport.publish(
            channel, self._agent_id, msg_type, content, metadata, queue=True
        )

    def queue_claim(self, channel: str, *, lease_seconds: int = 300) -> ClaimResult | None:
        """Claim the oldest unclaimed message from a channel.

        Args:
            channel: Channel to claim from.

        Returns:
            ClaimResult with the claimed message, or None if empty.
        """
        return self._transport.queue_claim(channel, self._agent_id, lease_seconds=lease_seconds)

    def queue_ack(self, message_id: str) -> ClaimResult | None:
        """Acknowledge a claimed message as completed.

        Args:
            message_id: ID of the message to ack.

        Returns:
            ClaimResult with status "completed", or None.
        """
        return self._transport.queue_ack(message_id, self._agent_id)

    # ── Sugar API: Notifications ──────────────────────────────────

    def notification_check(self) -> list[Message]:
        """Check for new notifications (polls with cursor).

        Returns:
            New notification messages since last check.
        """
        return self.channel_poll(f"_system:notifications:{self._agent_id}")

    # ── Reserved Methods (Not Yet Implemented) ────────────────────

    def rotate_secret(self) -> str:
        """Rotate the agent's secret. (Not yet implemented.)

        Returns:
            The new secret string.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("Secret rotation is not yet implemented.")

    def revoke(self) -> None:
        """Revoke this agent's registration. (Not yet implemented.)

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("Agent revocation is not yet implemented.")
