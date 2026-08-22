"""In-memory message backend for piazza bus."""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from piazza.types import AgentPresence, ClaimResult, Message


class MemoryBackend:
    """In-memory message backend for testing.

    Messages are stored in plain Python lists, protected by a lock for
    thread safety. Not suitable for cross-process communication.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: dict[str, list[Message]] = defaultdict(list)
        self._messages_by_id: dict[str, Message] = {}
        self._queue_status: dict[str, dict] = {}
        self._presence: dict[str, dict] = {}  # agent_id → {last_seen, metadata}

    def store(self, message: Message, *, queue: bool = False) -> None:
        """Store a message in memory.

        Args:
            message: Message to store.
            queue: If True, mark as claimable.
        """
        with self._lock:
            self._messages[message.channel].append(message)
            self._messages_by_id[message.id] = message
            if queue:
                self._queue_status[message.id] = {
                    "status": "unclaimed",
                    "claimed_by": None,
                    "claimed_at": None,
                }

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Retrieve messages from a channel.

        Args:
            channel: Channel to query.
            after: If provided, only return messages with ID > this value.
            limit: Maximum number of messages to return.
            msg_type: If provided, only return messages of this type.

        Returns:
            Messages in chronological order (oldest first).
        """
        with self._lock:
            msgs = list(self._messages.get(channel, []))
        if after:
            msgs = [m for m in msgs if m.id > after]
        if msg_type is not None:
            msgs = [m for m in msgs if m.msg_type == msg_type]
        return msgs[:limit]

    def list_channels(self) -> list[str]:
        """List all channels that have at least one message.

        Returns:
            Sorted list of channel names.
        """
        with self._lock:
            return sorted(ch for ch, msgs in self._messages.items() if msgs)

    def count_messages(self, channel: str | None = None) -> int:
        """Count messages, optionally filtered by channel.

        Args:
            channel: If provided, count only messages in this channel.

        Returns:
            Number of messages.
        """
        with self._lock:
            if channel:
                return len(self._messages.get(channel, []))
            return sum(len(msgs) for msgs in self._messages.values())

    def query_all(
        self,
        after: str | None = None,
        limit: int = 100,
        channel: str | None = None,
        sender: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Query messages across all channels with optional filters.

        Args:
            after: Cursor for pagination (message ID).
            limit: Maximum number of messages to return.
            channel: Filter by channel name.
            sender: Filter by sender.
            msg_type: Filter by message type.

        Returns:
            Messages in chronological order (oldest first).
        """
        with self._lock:
            if channel:
                msgs = list(self._messages.get(channel, []))
            else:
                msgs = [m for ch_msgs in self._messages.values() for m in ch_msgs]
                msgs.sort(key=lambda m: m.id)

        msgs = self._apply_filters(msgs, after=after, sender=sender, msg_type=msg_type)
        return msgs[:limit]

    @staticmethod
    def _apply_filters(
        msgs: list[Message],
        after: str | None = None,
        sender: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Apply optional filters to a message list."""
        if after:
            msgs = [m for m in msgs if m.id > after]
        if sender:
            msgs = [m for m in msgs if m.sender == sender]
        if msg_type is not None:
            msgs = [m for m in msgs if m.msg_type == msg_type]
        return msgs

    def get_stats(self) -> dict:
        """Return aggregate statistics for admin dashboard.

        Returns:
            Dict with total_messages, total_channels, total_senders,
            channel_breakdown, and msg_type_distribution.
        """
        with self._lock:
            all_msgs = [m for ch_msgs in self._messages.values() for m in ch_msgs]
            n_channels = len(self._messages)

        senders: set[str] = set()
        types: dict[str, int] = {}
        breakdown: dict[str, dict] = {}

        for m in all_msgs:
            senders.add(m.sender)
            types[m.msg_type] = types.get(m.msg_type, 0) + 1
            if m.channel not in breakdown:
                breakdown[m.channel] = {
                    "channel": m.channel,
                    "message_count": 0,
                    "last_message_time": m.timestamp,
                    "sender_count": 0,
                    "_senders": set(),
                }
            bd = breakdown[m.channel]
            bd["message_count"] += 1
            bd["_senders"].add(m.sender)
            if m.timestamp > bd["last_message_time"]:
                bd["last_message_time"] = m.timestamp

        for bd in breakdown.values():
            bd["sender_count"] = len(bd.pop("_senders"))

        return {
            "total_messages": len(all_msgs),
            "total_channels": n_channels,
            "total_senders": len(senders),
            "channel_breakdown": sorted(
                breakdown.values(),
                key=lambda x: x["message_count"],
                reverse=True,
            ),
            "msg_type_distribution": [
                {"msg_type": t, "count": c}
                for t, c in sorted(types.items(), key=lambda x: x[1], reverse=True)
            ],
        }

    def query_recent_timestamps(self, seconds: int = 60) -> list[str]:
        """Return timestamps of messages from the last N seconds.

        Args:
            seconds: Time window in seconds.

        Returns:
            List of ISO 8601 timestamp strings, sorted ascending.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        result = []
        with self._lock:
            for msgs in self._messages.values():
                for m in msgs:
                    if m.timestamp > cutoff:
                        result.append(m.timestamp)
        result.sort()
        return result

    def claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        now = datetime.now(timezone.utc)
        claimed_at = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock:
            for msg in self._messages.get(channel, []):
                qs = self._queue_status.get(msg.id)
                if not qs:
                    continue
                claimable = qs["status"] == "unclaimed" or (
                    qs["status"] == "claimed" and qs.get("lease_until", "") < claimed_at
                )
                if claimable:
                    qs["status"] = "claimed"
                    qs["claimed_by"] = claimed_by
                    qs["claimed_at"] = claimed_at
                    qs["lease_until"] = lease_until
                    return ClaimResult(
                        message=msg,
                        status="claimed",
                        claimed_by=claimed_by,
                        claimed_at=claimed_at,
                        lease_until=lease_until,
                    )
        return None

    def ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        with self._lock:
            qs = self._queue_status.get(message_id)
            if not qs or qs["status"] != "claimed" or qs["claimed_by"] != claimed_by:
                return None
            qs["status"] = "completed"
            msg = self._messages_by_id.get(message_id)
            if msg is None:
                return None
            return ClaimResult(
                message=msg,
                status="completed",
                claimed_by=qs["claimed_by"],
                claimed_at=qs["claimed_at"],
            )

    def get_queue_stats(self, channel: str | None = None) -> dict:
        result = {"unclaimed": 0, "claimed": 0, "completed": 0}
        with self._lock:
            if channel:
                ids = {m.id for m in self._messages.get(channel, [])}
                for mid, qs in self._queue_status.items():
                    if mid in ids and qs["status"] in result:
                        result[qs["status"]] += 1
            else:
                for qs in self._queue_status.values():
                    if qs["status"] in result:
                        result[qs["status"]] += 1
        return result

    def retire_completed(self, max_age_seconds: int = 86400, max_per_channel: int = 1000) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        with self._lock:
            to_remove = self._collect_retire_ids(cutoff, max_per_channel)
            return self._remove_messages(to_remove)

    def _collect_retire_ids(self, cutoff: str, max_per_channel: int) -> set[str]:
        to_remove: set[str] = set()
        for mid, qs in self._queue_status.items():
            if qs["status"] == "completed" and qs["claimed_at"] and qs["claimed_at"] < cutoff:
                to_remove.add(mid)
        per_channel: dict[str, list[str]] = defaultdict(list)
        for ch, msgs in self._messages.items():
            for msg in msgs:
                qs = self._queue_status.get(msg.id)
                if qs and qs["status"] == "completed" and msg.id not in to_remove:
                    per_channel[ch].append(msg.id)
        for _ch, ids in per_channel.items():
            if len(ids) > max_per_channel:
                to_remove.update(ids[: len(ids) - max_per_channel])
        return to_remove

    def _remove_messages(self, to_remove: set[str]) -> int:
        deleted = 0
        for ch in list(self._messages):
            before = len(self._messages[ch])
            self._messages[ch] = [m for m in self._messages[ch] if m.id not in to_remove]
            deleted += before - len(self._messages[ch])
        for mid in to_remove:
            self._queue_status.pop(mid, None)
            self._messages_by_id.pop(mid, None)
        return deleted

    @staticmethod
    def _dedup_per_sender(msgs: list[Message]) -> list[Message]:
        """Keep only the latest message per sender."""
        seen: set[str] = set()
        kept: list[Message] = []
        for m in reversed(msgs):
            if m.sender not in seen:
                seen.add(m.sender)
                kept.append(m)
        kept.reverse()
        return kept

    def _trim_to_limit(self, msgs: list[Message], max_messages: int) -> list[Message]:
        """Trim *msgs* to *max_messages*, cleaning the id index."""
        if len(msgs) <= max_messages:
            return msgs
        for m in msgs[:-max_messages]:
            self._messages_by_id.pop(m.id, None)
        return msgs[-max_messages:]

    def _cleanup_deduped_index(self, channel: str, kept_msgs: list[Message]) -> None:
        """Remove index entries for messages no longer in *kept_msgs*."""
        kept_ids = {m.id for m in kept_msgs}
        for m in self._messages.get(channel, []):
            if m.id not in kept_ids:
                self._messages_by_id.pop(m.id, None)

    def compact(
        self,
        channel: str,
        *,
        max_messages: int | None = None,
        keep_latest_per_sender: bool = False,
    ) -> int:
        """Compact a channel by removing old messages.

        Args:
            channel: Channel to compact.
            max_messages: If set, keep only the latest *max_messages*
                messages (after per-sender dedup if enabled).
            keep_latest_per_sender: If True, keep only the latest
                message per sender, removing older duplicates.

        Returns:
            Number of messages removed.
        """
        if max_messages is not None and max_messages < 1:
            raise ValueError("max_messages must be at least 1")
        with self._lock:
            msgs = self._messages.get(channel)
            if not msgs:
                return 0

            original_count = len(msgs)

            if keep_latest_per_sender:
                msgs = self._dedup_per_sender(msgs)

            if max_messages is not None:
                msgs = self._trim_to_limit(msgs, max_messages)

            if keep_latest_per_sender:
                self._cleanup_deduped_index(channel, msgs)

            self._messages[channel] = msgs
            return original_count - len(msgs)

    def get_backend_info(self) -> dict:
        """Return backend type and usage info."""
        import sys

        with self._lock:
            total_msgs = sum(len(msgs) for msgs in self._messages.values())
            n_channels = len(self._messages)
            size_bytes = sys.getsizeof(self._messages)
            for msgs in self._messages.values():
                size_bytes += sys.getsizeof(msgs)
                for m in msgs:
                    size_bytes += sys.getsizeof(m)

        return {
            "type": "memory",
            "total_messages": total_msgs,
            "total_channels": n_channels,
            "estimated_size_bytes": size_bytes,
            "estimated_size_mb": round(size_bytes / (1024 * 1024), 2),
        }

    # ── Presence ──────────────────────────────────────────────

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._presence[agent_id] = {
                "last_seen": now,
                "metadata": metadata,
            }

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        """Return all known agents with computed online/offline status."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        result: list[AgentPresence] = []
        with self._lock:
            for agent_id, rec in self._presence.items():
                status = "online" if rec["last_seen"] >= cutoff else "offline"
                result.append(
                    AgentPresence(
                        agent_id=agent_id,
                        status=status,
                        last_seen=rec["last_seen"],
                        metadata=rec["metadata"],
                    )
                )
        result.sort(key=lambda a: a.agent_id)
        return result

    def agent_status(self, agent_id: str, timeout_seconds: int = 120) -> AgentPresence | None:
        """Return presence for a single agent, or ``None`` if unknown."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        with self._lock:
            rec = self._presence.get(agent_id)
            if rec is None:
                return None
            status = "online" if rec["last_seen"] >= cutoff else "offline"
            return AgentPresence(
                agent_id=agent_id,
                status=status,
                last_seen=rec["last_seen"],
                metadata=rec["metadata"],
            )

    def close(self) -> None:
        """Clear all stored messages."""
        with self._lock:
            self._messages.clear()
            self._messages_by_id.clear()
            self._queue_status.clear()
            self._presence.clear()

    def __repr__(self) -> str:
        return "MemoryBackend()"
