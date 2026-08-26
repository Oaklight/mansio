"""NATS JetStream message backend for mansio bus.

Uses NATS with JetStream for distributed message persistence and
real-time pub/sub. Each mansio channel maps to a JetStream subject
under a configurable prefix. Messages are stored in a JetStream stream
with file-based persistence.

Requires the ``nats-py`` package::

    pip install nats-py

Example::

    backend = NATSBackend("nats://localhost:4222")
    backend.connect()
    bus = Bus(backend=backend)

Note:
    Importing this module directly requires ``nats-py``.  The lazy
    import in ``mansio.backends.__init__`` guards against this for
    code that only uses ``from mansio.backends import NATSBackend``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mansio.protocols import Backend, Compactable, Presenceable
from mansio.types import AgentPresence, ClaimResult, Message

try:
    import nats
    import nats.errors
    from nats.aio.client import Client as NATSClient
    from nats.js.api import (
        ConsumerConfig,
        DeliverPolicy,
        KeyValueConfig,
        RetentionPolicy,
        StorageType,
        StreamConfig,
    )
    from nats.js.client import JetStreamContext
    from nats.js.errors import (
        KeyNotFoundError,
        KeyWrongLastSequenceError,
        NoKeysError,
    )
except ImportError as exc:
    raise ImportError("NATS backend requires 'nats-py'. Install with: pip install nats-py") from exc

logger = logging.getLogger(__name__)

# Default cap when callers request "all" messages (limit=0).
# JetStream has no native "give me everything" fetch, so we use a high
# but bounded default to avoid unbounded memory growth.
_DEFAULT_FETCH_CAP = 10_000


class NATSBackend(Backend, Presenceable, Compactable):
    """NATS JetStream-backed message backend.

    Provides durable, distributed message persistence via NATS JetStream.
    Each mansio channel maps to a subject ``<prefix>.<channel>`` in a
    single JetStream stream.

    The backend runs an internal asyncio event loop on a background
    thread to bridge sync calls from the Bus to async NATS operations.

    Args:
        url: NATS server URL(s). Comma-separated for clusters.
        stream_name: JetStream stream name for mansio messages.
        subject_prefix: Subject prefix. Channels become ``<prefix>.<channel>``.
        max_msgs: Maximum messages to retain per stream (0 = unlimited).
        max_bytes: Maximum stream size in bytes (0 = unlimited).
        storage: JetStream storage type ("file" or "memory").
        connect_timeout: Connection timeout in seconds.
        operation_timeout: Timeout for individual async operations in seconds.
    """

    def __init__(
        self,
        url: str = "nats://localhost:4222",
        *,
        stream_name: str = "MANSIO",
        subject_prefix: str = "mansio",
        max_msgs: int = 0,
        max_bytes: int = 0,
        storage: str = "file",
        connect_timeout: float = 5.0,
        operation_timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._stream_name = stream_name
        self._subject_prefix = subject_prefix
        self._max_msgs = max_msgs
        self._max_bytes = max_bytes
        self._storage = StorageType.FILE if storage == "file" else StorageType.MEMORY
        self._connect_timeout = connect_timeout
        self._operation_timeout = operation_timeout

        self._nc: NATSClient | None = None
        self._js: JetStreamContext | None = None
        self._kv: Any = None

        # Guards the event loop lifecycle (_loop, _loop_thread, _connected).
        self._loop_lock = threading.Lock()
        # Guards the in-memory presence store.
        self._presence_lock = threading.Lock()

        # Background event loop for async operations
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._connected = False

        # In-memory presence store (not persisted to NATS)
        self._presence: dict[str, dict] = {}

        # In-memory message index for get_message() lookups.
        # Only contains messages stored through this backend instance.
        self._messages_by_id: dict[str, Message] = {}

    # ── Subject encoding ──────────────────────────────────────

    def _subject(self, channel: str) -> str:
        """Map a mansio channel name to a NATS subject.

        Uses percent-encoding for lossless round-tripping.
        Characters that conflict with NATS subject hierarchy
        (``.``, ``>``, ``*``, `` ``) are percent-encoded.
        """
        safe_channel = urllib.parse.quote(channel, safe="-_~")
        return f"{self._subject_prefix}.{safe_channel}"

    def _channel_from_subject(self, subject: str) -> str:
        """Extract mansio channel name from a NATS subject."""
        prefix = f"{self._subject_prefix}."
        if subject.startswith(prefix):
            return urllib.parse.unquote(subject[len(prefix) :])
        return subject

    # ── Serialization ─────────────────────────────────────────

    @staticmethod
    def _msg_to_nats_payload(message: Message) -> bytes:
        """Serialize a mansio Message to NATS payload bytes."""
        data = {
            "id": message.id,
            "channel": message.channel,
            "sender": message.sender,
            "msg_type": message.msg_type,
            "payload": message.payload,
            "timestamp": message.timestamp,
            "metadata": message.metadata,
            "parent_id": message.parent_id,
            "thread_id": message.thread_id,
            "intent": message.intent,
        }
        return json.dumps(data).encode("utf-8")

    @staticmethod
    def _nats_payload_to_msg(data: bytes) -> Message:
        """Deserialize NATS payload bytes to a mansio Message."""
        d = json.loads(data.decode("utf-8"))
        return Message(
            id=d["id"],
            channel=d["channel"],
            sender=d["sender"],
            msg_type=d["msg_type"],
            payload=d["payload"],
            timestamp=d["timestamp"],
            metadata=d.get("metadata"),
            parent_id=d.get("parent_id"),
            thread_id=d.get("thread_id"),
            intent=d.get("intent"),
        )

    # ── Event loop bridge ─────────────────────────────────────

    def _start_loop(self) -> None:
        """Start the background asyncio event loop thread.

        Caller must hold ``_loop_lock``.
        """
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="mansio-nats-loop",
        )
        self._loop_thread.start()

    def _run_async(self, coro: Any, *, timeout: float | None = None) -> Any:
        """Run an async coroutine from sync context.

        Thread-safe: guards the event loop lifecycle with ``_loop_lock``.
        """
        with self._loop_lock:
            if self._loop is None or self._loop.is_closed():
                self._start_loop()
            loop = self._loop
        assert loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout or self._operation_timeout)

    def _ensure_connected(self) -> None:
        """Connect if not already connected.

        Auto-connect is a convenience — the first operation triggers
        connection lazily.  For fail-fast behavior, call ``connect()``
        explicitly after construction.
        """
        with self._loop_lock:
            if self._connected:
                return
            self._start_loop()
        # _run_async + connect happen outside the lock to avoid
        # holding it during network I/O.
        self._run_async(self._async_connect())
        with self._loop_lock:
            self._connected = True

    # ── Connection ────────────────────────────────────────────

    async def _async_connect(self) -> None:
        """Establish NATS connection and ensure JetStream stream exists."""
        self._nc = await nats.connect(
            self._url,
            connect_timeout=self._connect_timeout,
        )
        self._js = self._nc.jetstream()

        # Create or update the stream
        stream_config = StreamConfig(
            name=self._stream_name,
            subjects=[f"{self._subject_prefix}.>"],
            retention=RetentionPolicy.LIMITS,
            storage=self._storage,
            max_msgs=self._max_msgs,
            max_bytes=self._max_bytes,
        )
        try:
            await self._js.find_stream_info_by_subject(f"{self._subject_prefix}.>")
            try:
                await self._js.update_stream(stream_config)
            except Exception:
                # update_stream can fail if config changes are incompatible
                # (e.g. switching storage type).  Log but don't crash — the
                # existing stream is still usable.
                logger.warning(
                    "Failed to update stream %s config; using existing",
                    self._stream_name,
                    exc_info=True,
                )
        except Exception:
            await self._js.add_stream(stream_config)

        # KV bucket for queue claim state
        kv_bucket = f"{self._stream_name}-queue-state"
        try:
            self._kv = await self._js.key_value(kv_bucket)
        except Exception:
            self._kv = await self._js.create_key_value(
                KeyValueConfig(bucket=kv_bucket, storage=self._storage)
            )

    def connect(self) -> None:
        """Connect to NATS server (sync wrapper).

        Must be called before using the backend, or operations will
        auto-connect on first use.
        """
        with self._loop_lock:
            self._start_loop()
        self._run_async(self._async_connect())
        with self._loop_lock:
            self._connected = True

    # ── Store ─────────────────────────────────────────────────

    def store(self, message: Message) -> None:
        """Publish a message to NATS JetStream.

        Args:
            message: Message to store.

        Raises:
            Exception: On NATS connection or publish failure.
        """
        self._ensure_connected()

        subject = self._subject(message.channel)
        payload = self._msg_to_nats_payload(message)

        async def _pub() -> None:
            assert self._js is not None
            await self._js.publish(subject, payload)

        self._run_async(_pub())
        self._messages_by_id[message.id] = message

    def store_queue(self, message: Message) -> None:
        """Store a queue message to NATS JetStream with claim tracking.

        Note: publish + KV create are not transactional. If KV create
        fails after a successful publish, the message remains in the
        stream (visible to query) but is invisible to queue operations.
        """
        self._ensure_connected()

        subject = self._subject(message.channel)
        payload = self._msg_to_nats_payload(message)

        async def _pub_queue() -> None:
            assert self._js is not None and self._kv is not None
            ack = await self._js.publish(subject, payload)
            state = {
                "status": "unclaimed",
                "channel": message.channel,
                "seq": ack.seq,
                "claimed_by": None,
                "claimed_at": None,
                "lease_until": None,
            }
            await self._kv.create(message.id, json.dumps(state).encode())

        self._run_async(_pub_queue())
        self._messages_by_id[message.id] = message

    # ── Fetch helpers ─────────────────────────────────────────

    async def _fetch_batch(self, sub: Any, batch_size: int) -> list[Any] | None:
        """Fetch a single batch from a pull subscription.

        Returns the batch list, or None to signal end-of-stream.
        """
        try:
            batch = await sub.fetch(batch=batch_size, timeout=0.5)
        except (asyncio.TimeoutError, nats.errors.TimeoutError):
            # Normal end-of-stream: no more messages to fetch.
            return None
        return batch if batch else None

    async def _drain_subscription(
        self,
        sub: Any,
        *,
        limit: int,
        after: str | None = None,
        sender: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Pull batches from *sub* until exhausted or *limit* reached."""
        messages: list[Message] = []
        batch_size = min(limit, 100)
        while True:
            batch = await self._fetch_batch(sub, batch_size)
            if batch is None:
                break
            for msg in batch:
                mansio_msg = self._nats_payload_to_msg(msg.data)
                if self._matches(mansio_msg, after=after, sender=sender, msg_type=msg_type):
                    messages.append(mansio_msg)
                    if len(messages) >= limit:
                        return messages
        return messages

    async def _fetch_all(
        self,
        subject: str,
        *,
        limit: int = 0,
        after: str | None = None,
        sender: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Fetch messages from a JetStream subject using pull consumer.

        Uses pull_subscribe + fetch(batch) for efficient retrieval.

        When *limit* is 0 (meaning "all"), an internal cap of
        ``_DEFAULT_FETCH_CAP`` is applied to prevent unbounded memory
        growth.  Callers needing truly unbounded scans should paginate
        with ``after`` cursors.

        Raises:
            Exception: On NATS connection or subscription failure.
        """
        assert self._js is not None
        effective_limit = limit if limit > 0 else _DEFAULT_FETCH_CAP

        sub = await self._js.pull_subscribe(subject, stream=self._stream_name)
        try:
            return await self._drain_subscription(
                sub, limit=effective_limit, after=after, sender=sender, msg_type=msg_type
            )
        finally:
            await sub.unsubscribe()

    @staticmethod
    def _matches(
        msg: Message,
        *,
        after: str | None = None,
        sender: str | None = None,
        msg_type: str | None = None,
    ) -> bool:
        """Check if a message passes the given filters."""
        if after and msg.id <= after:
            return False
        if sender and msg.sender != sender:
            return False
        return not (msg_type and msg.msg_type != msg_type)

    # ── Message lookup ────────────────────────────────────────

    def get_message(self, message_id: str) -> Message | None:
        """Look up a message by ID from the in-memory index.

        Only messages stored through this backend instance are indexed.
        Returns None if the message is not found.
        """
        return self._messages_by_id.get(message_id)

    # ── Query ─────────────────────────────────────────────────

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
        msg_type: str | None = None,
        order: Literal["oldest", "newest"] = "oldest",
        thread_id: str | None = None,
        intent: str | None = None,
        offset: int = 0,
    ) -> list[Message]:
        """Retrieve messages from a channel via JetStream.

        Args:
            channel: Channel to query.
            after: If provided, only return messages with ID > this value.
            limit: Maximum number of messages to return.
            msg_type: If provided, only return messages of this type.
            order: ``"oldest"`` (default) or ``"newest"``.
                Currently only ``"oldest"`` is implemented.
            thread_id: If provided, only return messages in this thread.
            intent: If provided, only return messages with this intent.
            offset: Number of messages to skip before returning results.

        Returns:
            Messages in chronological order (oldest first).

        Raises:
            Exception: On NATS connection or fetch failure.
        """
        if order != "oldest":
            logger.warning(
                "NATSBackend.query() does not support order=%r yet; returning oldest-first", order
            )
        self._ensure_connected()
        subject = self._subject(channel)
        # NATS JetStream doesn't support server-side thread_id/intent/offset filtering,
        # so we fetch more and filter client-side when needed.
        needs_client_filter = thread_id is not None or intent is not None
        fetch_limit = limit + offset if not needs_client_filter else (limit + offset) * 5
        msgs = self._run_async(
            self._fetch_all(subject, limit=fetch_limit, after=after, msg_type=msg_type)
        )
        if thread_id is not None:
            msgs = [m for m in msgs if m.thread_id == thread_id]
        if intent is not None:
            msgs = [m for m in msgs if m.intent == intent]
        if offset:
            msgs = msgs[offset:]
        return msgs[:limit]

    def list_channels(self) -> list[str]:
        """List all channels using JetStream stream info.

        Uses the stream info API to get subject counts without
        consuming all messages.

        Returns:
            Sorted list of channel names.
        """
        self._ensure_connected()

        async def _list() -> list[str]:
            assert self._js is not None
            channels: list[str] = []
            stream_info = await self._js.stream_info(self._stream_name)
            state = stream_info.state
            # state.subjects is a dict of subject → message count
            if hasattr(state, "subjects") and state.subjects:
                for subj in state.subjects:
                    channels.append(self._channel_from_subject(subj))
            else:
                # Fallback: scan messages if subjects not available
                logger.warning(
                    "stream_info.subjects unavailable for %s, falling back to message scan",
                    self._stream_name,
                )
                seen: set[str] = set()
                msgs = await self._fetch_all(f"{self._subject_prefix}.>")
                for m in msgs:
                    if m.channel not in seen:
                        seen.add(m.channel)
                        channels.append(m.channel)
            return sorted(channels)

        return self._run_async(_list())

    def message_count(self, channel: str | None = None) -> int:
        """Count messages using JetStream stream info.

        Args:
            channel: If provided, count only messages in this channel.

        Returns:
            Number of messages.
        """
        self._ensure_connected()

        async def _count() -> int:
            assert self._js is not None
            stream_info = await self._js.stream_info(self._stream_name)
            state = stream_info.state

            if channel is None:
                return state.messages

            # Count for specific channel subject
            subject = self._subject(channel)
            if hasattr(state, "subjects") and state.subjects:
                return state.subjects.get(subject, 0)

            # Fallback: count by consuming
            msgs = await self._fetch_all(subject)
            return len(msgs)

        return self._run_async(_count())

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
        self._ensure_connected()
        subject = self._subject(channel) if channel else f"{self._subject_prefix}.>"
        return self._run_async(
            self._fetch_all(
                subject,
                limit=limit,
                after=after,
                sender=sender,
                msg_type=msg_type,
            )
        )

    def stats(self) -> dict:
        """Return aggregate statistics.

        Uses stream info for totals (``total_messages``,
        ``total_channels``) and falls back to message scanning for
        per-sender / per-type breakdowns.

        .. note::

            The per-channel/per-sender/per-type breakdowns scan up to
            ``_DEFAULT_FETCH_CAP`` messages.  For streams larger than
            that, the breakdowns will be approximate.

        Returns:
            Dict with total_messages, total_channels, total_senders,
            channel_breakdown, and msg_type_distribution.
        """
        self._ensure_connected()

        async def _stats() -> dict:
            assert self._js is not None

            # Use stream info for accurate totals
            stream_info = await self._js.stream_info(self._stream_name)
            state = stream_info.state
            total_messages = state.messages
            total_channels = state.num_subjects if hasattr(state, "num_subjects") else 0

            # Scan messages for per-sender/per-type breakdowns
            # NOTE: capped at _DEFAULT_FETCH_CAP messages
            all_msgs = await self._fetch_all(f"{self._subject_prefix}.>")

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
                "total_messages": total_messages,
                "total_channels": total_channels or len(breakdown),
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

        return self._run_async(_stats())

    def recent_timestamps(self, seconds: int = 60) -> list[str]:
        """Return timestamps of messages from the last N seconds.

        Uses a pull consumer with ``DeliverPolicy.BY_START_TIME`` for
        efficient time-windowed retrieval.

        Args:
            seconds: Time window in seconds.

        Returns:
            List of ISO 8601 timestamp strings, sorted ascending.
        """
        self._ensure_connected()

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()

        async def _recent() -> list[str]:
            assert self._js is not None
            result: list[str] = []

            deliver_time = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            config = ConsumerConfig(
                deliver_policy=DeliverPolicy.BY_START_TIME,
                opt_start_time=deliver_time.isoformat(),
            )
            sub = await self._js.pull_subscribe(
                f"{self._subject_prefix}.>",
                config=config,
                stream=self._stream_name,
            )
            try:
                while True:
                    batch = await self._fetch_batch(sub, batch_size=100)
                    if batch is None:
                        break
                    for msg in batch:
                        mansio_msg = self._nats_payload_to_msg(msg.data)
                        if mansio_msg.timestamp > cutoff:
                            result.append(mansio_msg.timestamp)
            finally:
                await sub.unsubscribe()

            result.sort()
            return result

        return self._run_async(_recent())

    def info(self) -> dict:
        """Return backend type and connection info."""
        if not self._connected:
            return {
                "type": "nats",
                "url": self._url,
                "stream": self._stream_name,
                "connected": False,
            }

        async def _info() -> dict:
            assert self._js is not None
            info_dict: dict[str, Any] = {
                "type": "nats",
                "url": self._url,
                "stream": self._stream_name,
                "subject_prefix": self._subject_prefix,
                "connected": True,
            }
            stream_info = await self._js.stream_info(self._stream_name)
            state = stream_info.state
            info_dict["total_messages"] = state.messages
            info_dict["total_bytes"] = state.bytes
            info_dict["total_channels"] = state.num_subjects
            info_dict["first_seq"] = state.first_seq
            info_dict["last_seq"] = state.last_seq
            return info_dict

        return self._run_async(_info())

    # ── Queue operations ──────────────────────────────────────

    async def _fetch_msg_by_seq(self, seq: int) -> Message:
        """Retrieve a message from the stream by its JetStream sequence number."""
        assert self._js is not None
        raw = await self._js.get_msg(self._stream_name, seq)
        assert raw.data is not None
        return self._nats_payload_to_msg(raw.data)

    async def _kv_scan(self, channel: str | None = None) -> list[tuple[str, Any, dict]]:
        """Scan KV bucket for queue entries, optionally filtered by channel.

        Returns list of (key, kv_entry, state_dict) tuples.

        Note: O(n) round-trips (keys + get per key). Acceptable for
        moderate queue depths; consider indexed lookups if this becomes
        a bottleneck.
        """
        assert self._kv is not None
        results: list[tuple[str, Any, dict]] = []
        try:
            keys = await self._kv.keys()
        except NoKeysError:
            return results
        for key in keys:
            try:
                entry = await self._kv.get(key)
            except KeyNotFoundError:
                continue
            state = json.loads(entry.value)
            if channel is not None and state.get("channel") != channel:
                continue
            results.append((key, entry, state))
        return results

    def queue_claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        """Claim the oldest unclaimed queue message via KV CAS."""
        self._ensure_connected()

        async def _claim() -> ClaimResult | None:
            now = datetime.now(timezone.utc)
            claimed_at = now.isoformat()
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()

            entries = await self._kv_scan(channel)
            candidates = []
            for key, entry, state in entries:
                # ISO 8601 strings with +00:00 suffix are lexicographically
                # comparable, so direct string comparison works for lease expiry.
                if state["status"] == "unclaimed" or (
                    state["status"] == "claimed" and (state.get("lease_until") or "") < claimed_at
                ):
                    candidates.append((key, entry, state))

            candidates.sort(key=lambda x: x[2]["seq"])

            for key, entry, state in candidates:
                new_state = {
                    **state,
                    "status": "claimed",
                    "claimed_by": claimed_by,
                    "claimed_at": claimed_at,
                    "lease_until": lease_until,
                }
                try:
                    await self._kv.update(key, json.dumps(new_state).encode(), last=entry.revision)
                except KeyWrongLastSequenceError:
                    continue
                msg = await self._fetch_msg_by_seq(state["seq"])
                return ClaimResult(
                    message=msg,
                    status="claimed",
                    claimed_by=claimed_by,
                    claimed_at=claimed_at,
                    lease_until=lease_until,
                )
            return None

        return self._run_async(_claim())

    def queue_ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Acknowledge a claimed message via KV CAS."""
        self._ensure_connected()

        async def _ack() -> ClaimResult | None:
            assert self._kv is not None
            try:
                entry = await self._kv.get(message_id)
            except KeyNotFoundError:
                return None
            state = json.loads(entry.value)
            if state["status"] != "claimed" or state["claimed_by"] != claimed_by:
                return None
            new_state = {**state, "status": "completed", "lease_until": None}
            try:
                await self._kv.update(
                    message_id, json.dumps(new_state).encode(), last=entry.revision
                )
            except KeyWrongLastSequenceError:
                return None
            msg = await self._fetch_msg_by_seq(state["seq"])
            return ClaimResult(
                message=msg,
                status="completed",
                claimed_by=claimed_by,
                claimed_at=state["claimed_at"],
            )

        return self._run_async(_ack())

    _QUEUE_STATUS_KEYS = ("status", "claimed_by", "claimed_at", "lease_until")

    def queue_status(self, message_id: str) -> dict | None:
        """Return queue status for a single message from KV."""
        self._ensure_connected()

        async def _status() -> dict | None:
            assert self._kv is not None
            try:
                entry = await self._kv.get(message_id)
            except KeyNotFoundError:
                return None
            state = json.loads(entry.value)
            return {k: state.get(k) for k in self._QUEUE_STATUS_KEYS}

        return self._run_async(_status())

    def queue_stats(self, channel: str | None = None) -> dict:
        """Return queue status counts by scanning KV entries."""
        self._ensure_connected()

        async def _stats() -> dict:
            counts: dict[str, int] = {"unclaimed": 0, "claimed": 0, "completed": 0}
            for _key, _entry, state in await self._kv_scan(channel):
                status = state.get("status", "unclaimed")
                counts[status] = counts.get(status, 0) + 1
            return counts

        return self._run_async(_stats())

    async def _kv_delete_with_stream(self, key: str, seq: int | None) -> None:
        """Delete a KV entry and its backing stream message."""
        assert self._js is not None and self._kv is not None
        if seq is not None:
            with contextlib.suppress(nats.errors.Error):
                await self._js.delete_msg(self._stream_name, seq)
        await self._kv.delete(key)

    async def _retire_by_age(self, cutoff: str) -> int:
        """Delete completed queue entries older than *cutoff*."""
        retired = 0
        for key, _entry, state in await self._kv_scan():
            if state["status"] == "completed" and (state.get("claimed_at") or "") < cutoff:
                await self._kv_delete_with_stream(key, state.get("seq"))
                retired += 1
        return retired

    async def _retire_by_count(self, max_per_channel: int) -> int:
        """Enforce per-channel cap on completed queue entries."""
        by_channel: dict[str, list[tuple[str, dict]]] = {}
        for key, _entry, state in await self._kv_scan():
            if state["status"] == "completed":
                by_channel.setdefault(state.get("channel", ""), []).append((key, state))

        retired = 0
        for items in by_channel.values():
            if len(items) <= max_per_channel:
                continue
            items.sort(key=lambda x: x[1].get("seq", 0))
            for key, state in items[: len(items) - max_per_channel]:
                await self._kv_delete_with_stream(key, state.get("seq"))
                retired += 1
        return retired

    def queue_retire(self, max_age_seconds: int = 86400, max_per_channel: int = 1000) -> int:
        """Remove old completed queue messages from KV and stream."""
        self._ensure_connected()

        async def _retire() -> int:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
            return await self._retire_by_age(cutoff) + await self._retire_by_count(max_per_channel)

        return self._run_async(_retire())

    # ── Presence ──────────────────────────────────────────────

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*."""
        now = datetime.now(timezone.utc).isoformat()
        with self._presence_lock:
            self._presence[agent_id] = {
                "last_seen": now,
                "metadata": metadata,
            }

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        """Return all known agents with computed online/offline status."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        result: list[AgentPresence] = []
        with self._presence_lock:
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
        with self._presence_lock:
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

    # ── Compaction ─────────────────────────────────────────────

    def compact(
        self,
        channel: str,
        *,
        max_messages: int | None = None,
        keep_latest_per_sender: bool = False,
    ) -> int:
        """Compact a channel (no-op for NATS backend).

        NATS JetStream manages its own retention policy. This method
        returns 0 without modifying the stream.

        Returns:
            Always 0.
        """
        return 0

    # ── Lifecycle ─────────────────────────────────────────────

    def close(self) -> None:
        """Close NATS connection and stop background loop."""
        with self._loop_lock:
            if self._nc is None or not self._connected:
                return

        async def _close() -> None:
            assert self._nc is not None
            await self._nc.close()

        with contextlib.suppress(Exception):
            self._run_async(_close())
        with self._loop_lock:
            self._connected = False

        with self._loop_lock:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._loop.stop)
                if self._loop_thread is not None:
                    self._loop_thread.join(timeout=5)
                self._loop.close()
                self._loop = None
                self._loop_thread = None

    def __repr__(self) -> str:
        return f"NATSBackend({self._url!r}, stream={self._stream_name!r})"
