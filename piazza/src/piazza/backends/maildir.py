"""Maildir message backend for piazza bus.

Uses Python's stdlib ``mailbox.Maildir`` for zero-dependency,
filesystem-based message persistence.  Each Piazza channel maps
to a separate Maildir directory.  Messages are stored as RFC 2822
emails with Piazza metadata in custom ``X-Piazza-*`` headers.

Directory layout::

    root/
      <channel-slug>/       # one Maildir per channel
        new/                # queue: unclaimed messages
        cur/                # regular messages + claimed/completed
        tmp/                # atomic delivery temp files

Queue semantics:
    - ``queue=True`` messages are delivered to ``new/``
    - ``claim()`` moves from ``new/`` → ``cur/`` with an ``F`` flag
      and records claim metadata in a sidecar ``.claim`` JSON file
    - ``ack()`` updates the sidecar status to ``completed``
    - Non-queue messages go directly to ``cur/``
"""

from __future__ import annotations

import email.message
import json
import mailbox
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from piazza.types import AgentPresence, ClaimResult, Message


def _channel_to_dirname(channel: str) -> str:
    """Sanitize a channel name for use as a directory name.

    Replaces characters unsafe for filesystems (``:``, ``/``, ``\\``,
    ``<``, ``>``, ``|``, ``?``, ``*``) with ``__``.
    """
    return re.sub(r'[:/\\<>|?*]', '__', channel)


def _dirname_to_channel(dirname: str) -> str | None:
    """Reverse a directory name back to a channel name.

    Returns None if no mapping file exists (handled by caller).
    This is a best-effort reverse; the authoritative mapping is
    stored in a ``_channel_name`` file inside each Maildir.
    """
    # This function is intentionally not used directly; the
    # ``_channel_name`` file is the source of truth.
    return None


def _msg_to_email(message: Message) -> email.message.EmailMessage:
    """Convert a Piazza Message to an RFC 2822 EmailMessage."""
    em = email.message.EmailMessage()
    em["X-Piazza-Id"] = message.id
    em["X-Piazza-Channel"] = message.channel
    em["X-Piazza-Sender"] = message.sender
    em["X-Piazza-MsgType"] = message.msg_type
    em["X-Piazza-Timestamp"] = message.timestamp
    if message.metadata:
        em["X-Piazza-Metadata"] = json.dumps(
            message.metadata, ensure_ascii=False, separators=(",", ":")
        )
    em["From"] = f"{message.sender}@piazza.local"
    em["Subject"] = f"[{message.channel}] {message.msg_type}"
    em["Date"] = message.timestamp
    em["Message-ID"] = f"<{message.id}@piazza.local>"
    em.set_content(message.payload)
    return em


def _email_to_msg(em: email.message.EmailMessage) -> Message | None:
    """Convert an RFC 2822 EmailMessage back to a Piazza Message.

    Returns None if required Piazza headers are missing.
    """
    msg_id = em.get("X-Piazza-Id")
    channel = em.get("X-Piazza-Channel")
    sender = em.get("X-Piazza-Sender")
    msg_type = em.get("X-Piazza-MsgType")
    timestamp = em.get("X-Piazza-Timestamp")
    if not all([msg_id, channel, sender, msg_type, timestamp]):
        return None

    meta_raw = em.get("X-Piazza-Metadata")
    metadata = json.loads(meta_raw) if meta_raw else None

    # mailbox.Maildir returns MaildirMessage (legacy email.message.Message),
    # which does not have get_content(). Use get_payload(decode=True) instead.
    payload_raw = em.get_payload(decode=True)
    if payload_raw is None:
        # Multipart or missing payload — try string fallback
        payload_raw = em.get_payload()
        if isinstance(payload_raw, list):
            # Should not happen for our single-part messages
            return None
    if isinstance(payload_raw, bytes):
        charset = em.get_content_charset() or "utf-8"
        payload = payload_raw.decode(charset, errors="replace")
    else:
        payload = str(payload_raw)
    # Strip trailing newline added by set_content/set_payload
    if payload.endswith("\n"):
        payload = payload[:-1]

    return Message(
        id=msg_id,
        channel=channel,
        sender=sender,
        msg_type=msg_type,
        payload=payload,
        timestamp=timestamp,
        metadata=metadata,
    )


class MaildirBackend:
    """Maildir-backed message backend.

    Each Piazza channel is stored as a separate Maildir directory
    under ``root_path``.  Messages use RFC 2822 format with custom
    ``X-Piazza-*`` headers for metadata.

    Args:
        root_path: Root directory for all channel Maildirs.
            Created automatically if it does not exist.
    """

    def __init__(self, root_path: str | Path) -> None:
        self._root = Path(root_path)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Cache of channel_name → Maildir instances
        self._maildirs: dict[str, mailbox.Maildir] = {}
        # Cache of dirname → channel_name (loaded from _channel_name files)
        self._channel_map: dict[str, str] = {}
        self._load_channel_map()

    def _load_channel_map(self) -> None:
        """Scan root directory and load channel name mappings."""
        if not self._root.exists():
            return
        for entry in self._root.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                name_file = entry / "_channel_name"
                if name_file.exists():
                    channel = name_file.read_text().strip()
                    self._channel_map[entry.name] = channel

    def _get_maildir(self, channel: str) -> mailbox.Maildir:
        """Get or create a Maildir for the given channel."""
        if channel in self._maildirs:
            return self._maildirs[channel]

        dirname = _channel_to_dirname(channel)
        md_path = self._root / dirname
        md = mailbox.Maildir(str(md_path), create=True)

        # Store authoritative channel name mapping
        name_file = md_path / "_channel_name"
        if not name_file.exists():
            name_file.write_text(channel)
            self._channel_map[dirname] = channel

        self._maildirs[channel] = md
        return md

    def _get_claim_path(self, channel: str, maildir_key: str) -> Path:
        """Get the path to a claim sidecar file."""
        dirname = _channel_to_dirname(channel)
        return self._root / dirname / f".claim.{maildir_key}.json"

    def _read_claim(self, channel: str, maildir_key: str) -> dict | None:
        """Read claim metadata from a sidecar file."""
        path = self._get_claim_path(channel, maildir_key)
        if path.exists():
            return json.loads(path.read_text())
        return None

    def _write_claim(self, channel: str, maildir_key: str, data: dict) -> None:
        """Write claim metadata to a sidecar file."""
        path = self._get_claim_path(channel, maildir_key)
        path.write_text(json.dumps(data, ensure_ascii=False))

    def _remove_claim(self, channel: str, maildir_key: str) -> None:
        """Remove a claim sidecar file."""
        path = self._get_claim_path(channel, maildir_key)
        if path.exists():
            path.unlink()

    def _all_messages(self, channel: str) -> list[Message]:
        """Read all messages from a channel's Maildir, sorted by ID."""
        md = self._get_maildir(channel)
        messages: list[Message] = []
        for key in md.iterkeys():
            em = md[key]
            msg = _email_to_msg(em)
            if msg is not None:
                messages.append(msg)
        messages.sort(key=lambda m: m.id)
        return messages

    def store(self, message: Message, *, queue: bool = False) -> None:
        """Persist a message to the channel's Maildir.

        Args:
            message: Message to store.
            queue: If True, deliver to ``new/`` (unclaimed).
        """
        em = _msg_to_email(message)
        with self._lock:
            md = self._get_maildir(message.channel)
            md_key = md.add(em)
            if not queue:
                # Move to cur/ immediately for non-queue messages
                msg_obj = md[md_key]
                msg_obj.add_flag("S")  # Seen flag → moves to cur/
                md[md_key] = msg_obj
            md.flush()

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
    ) -> list[Message]:
        """Retrieve messages from a channel.

        Args:
            channel: Channel to query.
            after: If provided, only return messages with ID > this value.
            limit: Maximum number of messages to return.

        Returns:
            Messages in chronological order (oldest first).
        """
        with self._lock:
            messages = self._all_messages(channel)
        if after:
            messages = [m for m in messages if m.id > after]
        return messages[:limit]

    def list_channels(self) -> list[str]:
        """List all channels that have at least one message.

        Returns:
            Sorted list of channel names.
        """
        with self._lock:
            channels = []
            self._load_channel_map()
            for dirname, channel in self._channel_map.items():
                md_path = self._root / dirname
                # Check if it has any messages
                md = mailbox.Maildir(str(md_path), create=False)
                if len(md) > 0:
                    channels.append(channel)
            return sorted(channels)

    def close(self) -> None:
        """Flush and close all Maildir instances."""
        with self._lock:
            for md in self._maildirs.values():
                md.flush()
                md.close()
            self._maildirs.clear()

    def count_messages(self, channel: str | None = None) -> int:
        """Count messages, optionally filtered by channel.

        Args:
            channel: If provided, count only messages in this channel.

        Returns:
            Number of messages.
        """
        with self._lock:
            if channel:
                md = self._get_maildir(channel)
                return len(md)
            total = 0
            self._load_channel_map()
            for dirname in self._channel_map:
                md_path = self._root / dirname
                if md_path.exists():
                    md = mailbox.Maildir(str(md_path), create=False)
                    total += len(md)
            return total

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
                messages = self._all_messages(channel)
            else:
                messages = []
                self._load_channel_map()
                for ch_name in self._channel_map.values():
                    messages.extend(self._all_messages(ch_name))
                messages.sort(key=lambda m: m.id)

        if after:
            messages = [m for m in messages if m.id > after]
        if sender:
            messages = [m for m in messages if m.sender == sender]
        if msg_type:
            messages = [m for m in messages if m.msg_type == msg_type]
        return messages[:limit]

    def get_stats(self) -> dict:
        """Return aggregate statistics for admin dashboard.

        Returns:
            Dict with total_messages, total_channels, total_senders,
            channel_breakdown, and msg_type_distribution.
        """
        with self._lock:
            self._load_channel_map()
            all_msgs: list[Message] = []
            for ch_name in self._channel_map.values():
                all_msgs.extend(self._all_messages(ch_name))

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

        n_channels = len({m.channel for m in all_msgs})

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
        with self._lock:
            self._load_channel_map()
            result: list[str] = []
            for ch_name in self._channel_map.values():
                for msg in self._all_messages(ch_name):
                    if msg.timestamp > cutoff:
                        result.append(msg.timestamp)
        result.sort()
        return result

    def claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        """Atomically claim the oldest unclaimed/lease-expired message.

        Unclaimed queue messages live in ``new/``.  Claiming moves
        them to ``cur/`` with the ``F`` (flagged) flag and creates a
        ``.claim`` sidecar JSON file with claim metadata.
        """
        now = datetime.now(timezone.utc)
        claimed_at = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()

        with self._lock:
            md = self._get_maildir(channel)
            # Collect candidates: messages in new/ (unclaimed) or
            # claimed with expired leases
            candidates: list[tuple[str, Message]] = []

            for key in md.iterkeys():
                em = md[key]
                msg = _email_to_msg(em)
                if msg is None:
                    continue

                claim_data = self._read_claim(channel, key)
                if claim_data is None:
                    # Check if in new/ (unclaimed queue message)
                    # Maildir messages in new/ have no flags
                    msg_obj = md[key]
                    flags = msg_obj.get_flags()
                    if "S" not in flags and "F" not in flags:
                        candidates.append((key, msg))
                elif claim_data.get("status") == "claimed":
                    # Check lease expiry
                    if claim_data.get("lease_until", "") < claimed_at:
                        candidates.append((key, msg))

            if not candidates:
                return None

            # Sort by message ID (time-ordered) and take oldest
            candidates.sort(key=lambda x: x[1].id)
            key, msg = candidates[0]

            # Move to cur/ with F flag
            msg_obj = md[key]
            msg_obj.set_flags("F")
            md[key] = msg_obj
            md.flush()

            # Write claim sidecar
            self._write_claim(channel, key, {
                "status": "claimed",
                "claimed_by": claimed_by,
                "claimed_at": claimed_at,
                "lease_until": lease_until,
                "message_id": msg.id,
            })

            return ClaimResult(
                message=msg,
                status="claimed",
                claimed_by=claimed_by,
                claimed_at=claimed_at,
            )

    def ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Mark a claimed message as completed."""
        with self._lock:
            # Search all channels for the message
            self._load_channel_map()
            for ch_name in self._channel_map.values():
                md = self._get_maildir(ch_name)
                for key in md.iterkeys():
                    em = md[key]
                    if em.get("X-Piazza-Id") != message_id:
                        continue

                    claim_data = self._read_claim(ch_name, key)
                    if (
                        claim_data is None
                        or claim_data.get("status") != "claimed"
                        or claim_data.get("claimed_by") != claimed_by
                    ):
                        return None

                    # Update claim to completed
                    claim_data["status"] = "completed"
                    self._write_claim(ch_name, key, claim_data)

                    # Update flags: add S (seen/completed)
                    msg_obj = md[key]
                    msg_obj.add_flag("S")
                    md[key] = msg_obj
                    md.flush()

                    msg = _email_to_msg(em)
                    if msg is None:
                        return None

                    return ClaimResult(
                        message=msg,
                        status="completed",
                        claimed_by=claim_data["claimed_by"],
                        claimed_at=claim_data["claimed_at"],
                    )
        return None

    def get_queue_stats(self, channel: str | None = None) -> dict:
        """Return queue status counts (unclaimed, claimed, completed)."""
        result = {"unclaimed": 0, "claimed": 0, "completed": 0}
        with self._lock:
            if channel:
                channels = [channel]
            else:
                self._load_channel_map()
                channels = list(self._channel_map.values())

            for ch_name in channels:
                md = self._get_maildir(ch_name)
                for key in md.iterkeys():
                    claim_data = self._read_claim(ch_name, key)
                    if claim_data is not None:
                        status = claim_data.get("status", "unclaimed")
                        if status in result:
                            result[status] += 1
                    else:
                        # Check if in new/ (unclaimed)
                        msg_obj = md[key]
                        flags = msg_obj.get_flags()
                        if "S" not in flags and "F" not in flags:
                            result["unclaimed"] += 1
        return result

    def retire_completed(
        self, max_age_seconds: int = 86400, max_per_channel: int = 1000
    ) -> int:
        """Remove old completed queue messages. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        deleted = 0

        with self._lock:
            self._load_channel_map()
            for ch_name in list(self._channel_map.values()):
                md = self._get_maildir(ch_name)
                to_remove: list[str] = []
                completed: list[tuple[str, str]] = []  # (key, claimed_at)

                for key in md.iterkeys():
                    claim_data = self._read_claim(ch_name, key)
                    if claim_data and claim_data.get("status") == "completed":
                        claimed_at = claim_data.get("claimed_at", "")
                        if claimed_at < cutoff:
                            to_remove.append(key)
                        else:
                            completed.append((key, claimed_at))

                # Also enforce max_per_channel
                if len(completed) > max_per_channel:
                    completed.sort(key=lambda x: x[1])
                    excess = completed[: len(completed) - max_per_channel]
                    to_remove.extend(k for k, _ in excess)

                for key in set(to_remove):
                    self._remove_claim(ch_name, key)
                    md.discard(key)
                    deleted += 1

                if to_remove:
                    md.flush()

        return deleted

    def get_backend_info(self) -> dict:
        """Return backend type, config, and usage info."""
        with self._lock:
            self._load_channel_map()
            total_msgs = 0
            n_channels = 0
            for dirname, ch_name in self._channel_map.items():
                md_path = self._root / dirname
                if md_path.exists():
                    md = mailbox.Maildir(str(md_path), create=False)
                    count = len(md)
                    if count > 0:
                        n_channels += 1
                        total_msgs += count

        # Calculate disk usage
        total_bytes = 0
        for dirpath, _dirnames, filenames in os.walk(self._root):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.isfile(fp):
                    total_bytes += os.path.getsize(fp)

        return {
            "type": "maildir",
            "root_path": str(self._root),
            "total_messages": total_msgs,
            "total_channels": n_channels,
            "disk_size_bytes": total_bytes,
            "disk_size_mb": round(total_bytes / (1024 * 1024), 2),
        }

    # ── Presence ──────────────────────────────────────────────

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*."""
        now = datetime.now(timezone.utc).isoformat()
        presence_file = self._root / ".presence.json"
        with self._lock:
            data: dict[str, dict] = {}
            if presence_file.exists():
                try:
                    data = json.loads(presence_file.read_text())
                except (json.JSONDecodeError, OSError):
                    data = {}
            data[agent_id] = {"last_seen": now, "metadata": metadata}
            presence_file.write_text(json.dumps(data, indent=2))

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        """Return all known agents with computed online/offline status."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        ).isoformat()
        presence_file = self._root / ".presence.json"
        with self._lock:
            if not presence_file.exists():
                return []
            try:
                data = json.loads(presence_file.read_text())
            except (json.JSONDecodeError, OSError):
                return []
        result: list[AgentPresence] = []
        for agent_id, rec in sorted(data.items()):
            status = "online" if rec.get("last_seen", "") >= cutoff else "offline"
            result.append(
                AgentPresence(
                    agent_id=agent_id,
                    status=status,
                    last_seen=rec.get("last_seen", ""),
                    metadata=rec.get("metadata"),
                )
            )
        return result

    def agent_status(
        self, agent_id: str, timeout_seconds: int = 120
    ) -> AgentPresence | None:
        """Return presence for a single agent, or ``None`` if unknown."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        ).isoformat()
        presence_file = self._root / ".presence.json"
        with self._lock:
            if not presence_file.exists():
                return None
            try:
                data = json.loads(presence_file.read_text())
            except (json.JSONDecodeError, OSError):
                return None
        rec = data.get(agent_id)
        if rec is None:
            return None
        status = "online" if rec.get("last_seen", "") >= cutoff else "offline"
        return AgentPresence(
            agent_id=agent_id,
            status=status,
            last_seen=rec.get("last_seen", ""),
            metadata=rec.get("metadata"),
        )

    def __repr__(self) -> str:
        return f"MaildirBackend({str(self._root)!r})"
