"""Maildir message backend for mansio bus.

Uses Python's stdlib ``mailbox.Maildir`` for zero-dependency,
filesystem-based message persistence.  Each Mansio channel maps
to a separate Maildir directory.  Messages are stored as RFC 2822
emails with Mansio metadata in custom ``X-Mansio-*`` headers.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mansio.protocols import Backend, Compactable, Presenceable
from mansio.types import AgentPresence, ClaimResult, Message


def _channel_to_dirname(channel: str) -> str:
    """Sanitize a channel name for use as a directory name.

    Replaces characters unsafe for filesystems (``:``, ``/``, ``\\``,
    ``<``, ``>``, ``|``, ``?``, ``*``) with ``__``.

    Also neutralises path-traversal components (``..``) and leading
    dots (which would create hidden directories skipped by the
    channel-map scanner).
    """
    name = re.sub(r"[:/\\<>|?*]", "__", channel)
    # Neutralise path-traversal: replace any ".." sequence with
    # a safe token, then strip leading dots (hidden directories).
    name = name.replace("..", "_dotdot_")
    name = name.lstrip(".")
    return name or "_empty_"


def _msg_to_email(message: Message) -> email.message.EmailMessage:
    """Convert a Mansio Message to an RFC 2822 EmailMessage."""
    em = email.message.EmailMessage()
    em["X-Mansio-Id"] = message.id
    em["X-Mansio-Channel"] = message.channel
    em["X-Mansio-Sender"] = message.sender
    em["X-Mansio-MsgType"] = message.msg_type
    em["X-Mansio-Timestamp"] = message.timestamp
    if message.metadata:
        em["X-Mansio-Metadata"] = json.dumps(
            message.metadata, ensure_ascii=False, separators=(",", ":")
        )
    em["From"] = f"{message.sender}@mansio.local"
    em["Subject"] = f"[{message.channel}] {message.msg_type}"
    em["Date"] = message.timestamp
    em["Message-ID"] = f"<{message.id}@mansio.local>"
    em.set_content(message.payload)
    return em


def _msg_to_queue_email(message: Message) -> email.message.EmailMessage:
    """Convert a Mansio Message to an RFC 2822 EmailMessage with queue marker."""
    em = _msg_to_email(message)
    em["X-Mansio-Queue"] = "true"
    return em


def _email_to_msg(em: email.message.Message) -> Message | None:
    """Convert an RFC 2822 Message (or MaildirMessage) back to a Mansio Message.

    Returns None if required Mansio headers are missing.
    """
    msg_id = em.get("X-Mansio-Id", "")
    channel = em.get("X-Mansio-Channel", "")
    sender = em.get("X-Mansio-Sender", "")
    msg_type = em.get("X-Mansio-MsgType", "")
    timestamp = em.get("X-Mansio-Timestamp", "")
    if not all([msg_id, channel, sender, msg_type, timestamp]):
        return None

    meta_raw = em.get("X-Mansio-Metadata")
    metadata = None
    if meta_raw:
        try:
            metadata = json.loads(meta_raw)
        except json.JSONDecodeError:
            metadata = None

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


class MaildirBackend(Backend, Presenceable, Compactable):
    """Maildir-backed message backend.

    Each Mansio channel is stored as a separate Maildir directory
    under ``root_path``.  Messages use RFC 2822 format with custom
    ``X-Mansio-*`` headers for metadata.

    .. warning:: **Single-process only.**  Although Maildir's on-disk
       format supports concurrent access by multiple processes, this
       backend caches ``Maildir`` instances whose internal key index
       may go stale if another process modifies the directory.  The
       ``threading.Lock`` serialises threads within one process but
       does not protect against cross-process races.  Do not run
       multiple ``mansio serve --maildir /same/path`` instances.

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
        self._channel_map_dirty = True
        # Index of message_id → (channel, maildir_key) for O(1) ack lookups
        self._msg_index: dict[str, tuple[str, str]] = {}
        self._load_channel_map()

    def _load_channel_map(self) -> None:
        """Scan root directory and reload channel name mappings.

        Only rescans the filesystem when the dirty flag is set.
        Call ``_invalidate_channel_map()`` after creating a new channel.
        """
        if not self._channel_map_dirty:
            return
        if not self._root.exists():
            return
        self._channel_map.clear()
        for entry in self._root.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                name_file = entry / "_channel_name"
                if name_file.exists():
                    channel = name_file.read_text().strip()
                    self._channel_map[entry.name] = channel
        self._channel_map_dirty = False

    def _invalidate_channel_map(self) -> None:
        """Mark the channel map as stale so the next read rescans."""
        self._channel_map_dirty = True

    def _get_maildir(self, channel: str) -> mailbox.Maildir:
        """Get or create a Maildir for the given channel.

        Raises:
            ValueError: If the sanitised directory name would escape
                the root path (path traversal).
        """
        if channel in self._maildirs:
            return self._maildirs[channel]

        dirname = _channel_to_dirname(channel)
        md_path = (self._root / dirname).resolve()
        if not md_path.is_relative_to(self._root.resolve()):
            raise ValueError(f"invalid channel name: {channel!r}")
        md = mailbox.Maildir(str(md_path), create=True)

        # Store authoritative channel name mapping
        name_file = md_path / "_channel_name"
        if not name_file.exists():
            name_file.write_text(channel)
            self._channel_map[dirname] = channel
            self._invalidate_channel_map()

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
        """Read all messages from a channel's Maildir, sorted by ID.

        Sorting by ``m.id`` assumes UUIDv7 IDs (time-ordered, so
        lexicographic sort == chronological sort).  This is a hard
        requirement of the Mansio protocol — ``Bus.publish()``
        generates UUIDv7 IDs for every message.
        """
        md = self._get_maildir(channel)
        messages: list[Message] = []
        for key in md.iterkeys():
            em = md[key]
            msg = _email_to_msg(em)
            if msg is not None:
                messages.append(msg)
        messages.sort(key=lambda m: m.id)
        return messages

    def store(self, message: Message) -> None:
        """Persist a regular (non-queue) message.

        Args:
            message: Message to store.
        """
        em = _msg_to_email(message)
        with self._lock:
            md = self._get_maildir(message.channel)
            md_key = md.add(em)
            self._msg_index[message.id] = (message.channel, md_key)
            # Move to cur/ immediately for non-queue messages
            msg_obj = md[md_key]
            msg_obj.add_flag("S")  # Seen flag → moves to cur/
            md[md_key] = msg_obj
            md.flush()

    def store_queue(self, message: Message) -> None:
        """Persist a message and mark it as claimable (queue semantics).

        Args:
            message: Message to store as a queue item.
        """
        em = _msg_to_queue_email(message)
        with self._lock:
            md = self._get_maildir(message.channel)
            md_key = md.add(em)
            self._msg_index[message.id] = (message.channel, md_key)
            md.flush()

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
            messages = self._all_messages(channel)
        if after:
            messages = [m for m in messages if m.id > after]
        if msg_type:
            messages = [m for m in messages if m.msg_type == msg_type]
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

    def message_count(self, channel: str | None = None) -> int:
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

    def search(
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

    def stats(self) -> dict:
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

    def recent_timestamps(self, seconds: int = 60) -> list[str]:
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

    def _collect_claim_candidates(
        self,
        channel: str,
        md: mailbox.Maildir,
        claimed_at: str,
    ) -> list[tuple[str, Message]]:
        """Collect unclaimed or lease-expired queue messages."""
        candidates: list[tuple[str, Message]] = []
        for key in md.iterkeys():
            em = md[key]
            msg = _email_to_msg(em)
            if msg is None:
                continue

            claim_data = self._read_claim(channel, key)
            if claim_data is None:
                if not self._is_unclaimed_queue_msg(em, md, key):
                    continue
                candidates.append((key, msg))
            elif self._is_lease_expired(claim_data, claimed_at):
                candidates.append((key, msg))
        return candidates

    @staticmethod
    def _is_unclaimed_queue_msg(
        em: email.message.Message,
        md: mailbox.Maildir,
        key: str,
    ) -> bool:
        """Return True if *em* is a queue message in new/ (unclaimed)."""
        if em.get("X-Mansio-Queue") != "true":
            return False
        flags = md[key].get_flags()
        return "S" not in flags and "F" not in flags

    @staticmethod
    def _is_lease_expired(claim_data: dict, now_iso: str) -> bool:
        """Return True if a claimed message's lease has expired."""
        if claim_data.get("status") != "claimed":
            return False
        return claim_data.get("lease_until", "") < now_iso

    def queue_claim(
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
            candidates = self._collect_claim_candidates(channel, md, claimed_at)
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
            self._write_claim(
                channel,
                key,
                {
                    "status": "claimed",
                    "claimed_by": claimed_by,
                    "claimed_at": claimed_at,
                    "lease_until": lease_until,
                    "message_id": msg.id,
                },
            )

            return ClaimResult(
                message=msg,
                status="claimed",
                claimed_by=claimed_by,
                claimed_at=claimed_at,
            )

    def queue_ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Mark a claimed message as completed."""
        with self._lock:
            return self._ack_inner(message_id, claimed_by)

    def _ack_inner(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Inner ack logic (caller must hold ``_lock``)."""
        ch_name, key = self._resolve_msg(message_id)
        if ch_name is None:
            return None

        md = self._get_maildir(ch_name)
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

        em = md[key]
        msg = _email_to_msg(em)
        if msg is None:
            return None

        return ClaimResult(
            message=msg,
            status="completed",
            claimed_by=claim_data["claimed_by"],
            claimed_at=claim_data["claimed_at"],
        )

    def _resolve_msg(self, message_id: str) -> tuple[str | None, str]:
        """Look up a message's channel and maildir key.

        Uses the in-memory index first, falls back to a full scan
        (cold start / restart).
        """
        # Fast path: index hit
        if message_id in self._msg_index:
            return self._msg_index[message_id]

        # Slow path: scan all channels (populates index for future)
        self._load_channel_map()
        for ch_name in self._channel_map.values():
            md = self._get_maildir(ch_name)
            for key in md.iterkeys():
                em = md[key]
                mid = em.get("X-Mansio-Id") or ""
                self._msg_index[mid] = (ch_name, key)
                if mid == message_id:
                    return ch_name, key

        return None, ""

    def queue_status(self, message_id: str) -> dict | None:
        """Return the queue status dict for a single message.

        Returns:
            Dict with 'status', 'claimed_by', 'claimed_at', etc.,
            or None if the message has no queue status.
        """
        with self._lock:
            channel, md_key = self._resolve_msg(message_id)
            if channel is None:
                return None
            # Check if this is a queue message
            md = self._get_maildir(channel)
            try:
                em = md[md_key]
            except KeyError:
                return None
            if em.get("X-Mansio-Queue") != "true":
                return None
            claim_data = self._read_claim(channel, md_key)
            if claim_data is None:
                return {
                    "status": "unclaimed",
                    "claimed_by": None,
                    "claimed_at": None,
                    "lease_until": None,
                }
            return {
                "status": claim_data.get("status", "claimed"),
                "claimed_by": claim_data.get("claimed_by"),
                "claimed_at": claim_data.get("claimed_at"),
                "lease_until": claim_data.get("lease_until"),
            }

    def _count_queue_channel(self, ch_name: str, result: dict) -> None:
        """Count queue status for a single channel into *result*."""
        md = self._get_maildir(ch_name)
        for key in md.iterkeys():
            claim_data = self._read_claim(ch_name, key)
            if claim_data is not None:
                status = claim_data.get("status", "unclaimed")
                if status in result:
                    result[status] += 1
            else:
                msg_obj = md[key]
                flags = msg_obj.get_flags()
                if "S" not in flags and "F" not in flags:
                    result["unclaimed"] += 1

    def queue_stats(self, channel: str | None = None) -> dict:
        """Return queue status counts (unclaimed, claimed, completed)."""
        result = {"unclaimed": 0, "claimed": 0, "completed": 0}
        with self._lock:
            if channel:
                channels = [channel]
            else:
                self._load_channel_map()
                channels = list(self._channel_map.values())

            for ch_name in channels:
                self._count_queue_channel(ch_name, result)
        return result

    def _collect_retirable_keys(
        self, ch_name: str, md: mailbox.Maildir, cutoff: str, max_per_channel: int
    ) -> list[str]:
        """Identify completed queue message keys eligible for retirement."""
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

        # Enforce max_per_channel
        if len(completed) > max_per_channel:
            completed.sort(key=lambda x: x[1])
            excess = completed[: len(completed) - max_per_channel]
            to_remove.extend(k for k, _ in excess)

        return to_remove

    def queue_retire(self, max_age_seconds: int = 86400, max_per_channel: int = 1000) -> int:
        """Remove old completed queue messages. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        deleted = 0

        with self._lock:
            self._load_channel_map()
            for ch_name in list(self._channel_map.values()):
                md = self._get_maildir(ch_name)
                to_remove = self._collect_retirable_keys(ch_name, md, cutoff, max_per_channel)

                for key in set(to_remove):
                    self._remove_claim(ch_name, key)
                    md.discard(key)
                    deleted += 1

                if to_remove:
                    md.flush()

        return deleted

    def info(self) -> dict:
        """Return backend type, config, and usage info."""
        with self._lock:
            self._load_channel_map()
            total_msgs = 0
            n_channels = 0
            for dirname, _ch_name in self._channel_map.items():
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

    # ── Presence ──────────────────────────────────────────────────

    _PRESENCE_FILE = "_presence.json"

    def _presence_path(self) -> Path:
        return self._root / self._PRESENCE_FILE

    def _load_presence(self) -> dict[str, dict]:
        """Load presence data from sidecar JSON."""
        path = self._presence_path()
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_presence(self, data: dict[str, dict]) -> None:
        """Persist presence data to sidecar JSON."""
        self._presence_path().write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*.

        Upserts ``last_seen`` to now and stores optional *metadata*.
        Presence is persisted to ``_presence.json`` under the root
        directory so it survives restarts.
        """
        with self._lock:
            data = self._load_presence()
            entry = data.get(agent_id, {})
            entry["last_seen"] = datetime.now(timezone.utc).isoformat()
            if metadata is not None:
                entry["metadata"] = metadata
            data[agent_id] = entry
            self._save_presence(data)

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        """Return all known agents with computed online/offline status."""
        with self._lock:
            data = self._load_presence()

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=timeout_seconds)
        result: list[AgentPresence] = []
        for agent_id in sorted(data):  # sorted by agent_id
            entry = data[agent_id]
            last_seen = entry.get("last_seen", "")
            try:
                ls_dt = datetime.fromisoformat(last_seen)
            except (ValueError, TypeError):
                ls_dt = datetime.min.replace(tzinfo=timezone.utc)
            status = "online" if ls_dt >= cutoff else "offline"
            meta = entry.get("metadata")
            result.append(
                AgentPresence(
                    agent_id=agent_id,
                    status=status,
                    last_seen=last_seen,
                    metadata=meta,
                )
            )
        return result

    def agent_status(self, agent_id: str, timeout_seconds: int = 120) -> AgentPresence | None:
        """Return presence for a single agent, or ``None`` if unknown."""
        with self._lock:
            data = self._load_presence()

        entry = data.get(agent_id)
        if entry is None:
            return None

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=timeout_seconds)
        last_seen = entry.get("last_seen", "")
        try:
            ls_dt = datetime.fromisoformat(last_seen)
        except (ValueError, TypeError):
            ls_dt = datetime.min.replace(tzinfo=timezone.utc)
        status = "online" if ls_dt >= cutoff else "offline"
        return AgentPresence(
            agent_id=agent_id,
            status=status,
            last_seen=last_seen,
            metadata=entry.get("metadata"),
        )

    # ── Compaction ────────────────────────────────────────────────

    @staticmethod
    def _filter_latest_per_sender(msgs: list[Message]) -> list[Message]:
        """Keep only the latest message per sender (preserving order)."""
        seen: set[str] = set()
        kept: list[Message] = []
        for m in reversed(msgs):
            if m.sender not in seen:
                seen.add(m.sender)
                kept.append(m)
        kept.reverse()
        return kept

    def _build_id_to_key(self, md: mailbox.Maildir) -> dict[str, str]:
        """Build a message-ID → Maildir-key index."""
        id_to_key: dict[str, str] = {}
        for key in md.iterkeys():
            em = md[key]
            mid = em.get("X-Mansio-Id", "")
            if mid:
                id_to_key[mid] = key
        return id_to_key

    def _remove_excess(
        self,
        msgs: list[Message],
        keep_ids: set[str],
        id_to_key: dict[str, str],
        md: mailbox.Maildir,
    ) -> int:
        """Delete messages not in *keep_ids* and flush."""
        removed = 0
        for m in msgs:
            if m.id in keep_ids:
                continue
            key = id_to_key.get(m.id)
            if key:
                md.discard(key)
                self._msg_index.pop(m.id, None)
                removed += 1
        if removed:
            md.flush()
        return removed

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
            max_messages: If set, keep only the latest *max_messages*.
            keep_latest_per_sender: If True, keep only the latest
                message per sender.

        Returns:
            Number of messages removed.
        """
        with self._lock:
            msgs = self._all_messages(channel)
            if not msgs:
                return 0

            md = self._get_maildir(channel)
            id_to_key = self._build_id_to_key(md)

            keep_msgs = list(msgs)
            if keep_latest_per_sender:
                keep_msgs = self._filter_latest_per_sender(keep_msgs)
            if max_messages is not None and len(keep_msgs) > max_messages:
                keep_msgs = keep_msgs[-max_messages:]

            keep_ids = {m.id for m in keep_msgs}
            return self._remove_excess(msgs, keep_ids, id_to_key, md)

    def __repr__(self) -> str:
        return f"MaildirBackend({str(self._root)!r})"
