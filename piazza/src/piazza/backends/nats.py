"""NATS JetStream message backend for piazza bus.

Uses NATS with JetStream for distributed message persistence and
real-time pub/sub. Each piazza channel maps to a JetStream subject
under a configurable prefix. Messages are stored in a JetStream stream
with file-based persistence.

Requires the ``nats-py`` package::

    pip install nats-py

Example::

    backend = NATSBackend("nats://localhost:4222")
    await backend.connect()
    bus = Bus(backend=backend)
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from piazza.types import Message

try:
    import nats
    from nats.aio.client import Client as NATSClient
    from nats.js.api import (
        ConsumerConfig,
        DeliverPolicy,
        RetentionPolicy,
        StorageType,
        StreamConfig,
    )
    from nats.js.client import JetStreamContext
except ImportError as exc:
    raise ImportError(
        "NATS backend requires 'nats-py'. Install with: pip install nats-py"
    ) from exc


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Get or create an event loop for sync-to-async bridging."""
    try:
        loop = asyncio.get_running_loop()
        return loop
    except RuntimeError:
        pass
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


class NATSBackend:
    """NATS JetStream-backed message backend.

    Provides durable, distributed message persistence via NATS JetStream.
    Each piazza channel maps to a subject ``<prefix>.<channel>`` in a
    single JetStream stream.

    The backend runs an internal asyncio event loop on a background
    thread to bridge sync calls from the Bus to async NATS operations.

    Args:
        url: NATS server URL(s). Comma-separated for clusters.
        stream_name: JetStream stream name for piazza messages.
        subject_prefix: Subject prefix. Channels become ``<prefix>.<channel>``.
        max_msgs: Maximum messages to retain per stream (0 = unlimited).
        max_bytes: Maximum stream size in bytes (0 = unlimited).
        storage: JetStream storage type ("file" or "memory").
        connect_timeout: Connection timeout in seconds.
    """

    def __init__(
        self,
        url: str = "nats://localhost:4222",
        *,
        stream_name: str = "PIAZZA",
        subject_prefix: str = "piazza",
        max_msgs: int = 0,
        max_bytes: int = 0,
        storage: str = "file",
        connect_timeout: float = 5.0,
    ) -> None:
        self._url = url
        self._stream_name = stream_name
        self._subject_prefix = subject_prefix
        self._max_msgs = max_msgs
        self._max_bytes = max_bytes
        self._storage = StorageType.FILE if storage == "file" else StorageType.MEMORY
        self._connect_timeout = connect_timeout

        self._nc: NATSClient | None = None
        self._js: JetStreamContext | None = None
        self._lock = threading.Lock()

        # Background event loop for async operations
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._connected = False

    def _subject(self, channel: str) -> str:
        """Map a piazza channel name to a NATS subject."""
        # Replace dots in channel names to avoid NATS subject collisions
        safe_channel = channel.replace(".", "_dot_").replace(" ", "_")
        return f"{self._subject_prefix}.{safe_channel}"

    def _channel_from_subject(self, subject: str) -> str:
        """Extract piazza channel name from a NATS subject."""
        prefix = f"{self._subject_prefix}."
        if subject.startswith(prefix):
            channel = subject[len(prefix) :]
            return channel.replace("_dot_", ".").replace("_", " ")
        return subject

    def _msg_to_nats_payload(self, message: Message) -> bytes:
        """Serialize a piazza Message to NATS payload bytes."""
        data = {
            "id": message.id,
            "channel": message.channel,
            "sender": message.sender,
            "msg_type": message.msg_type,
            "payload": message.payload,
            "timestamp": message.timestamp,
            "metadata": message.metadata,
        }
        return json.dumps(data).encode("utf-8")

    @staticmethod
    def _nats_payload_to_msg(data: bytes) -> Message:
        """Deserialize NATS payload bytes to a piazza Message."""
        d = json.loads(data.decode("utf-8"))
        return Message(
            id=d["id"],
            channel=d["channel"],
            sender=d["sender"],
            msg_type=d["msg_type"],
            payload=d["payload"],
            timestamp=d["timestamp"],
            metadata=d.get("metadata"),
        )

    def _start_loop(self) -> None:
        """Start the background asyncio event loop thread."""
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="piazza-nats-loop",
        )
        self._loop_thread.start()

    def _run_async(self, coro: Any) -> Any:
        """Run an async coroutine from sync context."""
        if self._loop is None or self._loop.is_closed():
            self._start_loop()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

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
            await self._js.update_stream(stream_config)
        except Exception:
            await self._js.add_stream(stream_config)

    def connect(self) -> None:
        """Connect to NATS server (sync wrapper).

        Must be called before using the backend.
        """
        self._start_loop()
        self._run_async(self._async_connect())
        self._connected = True

    def store(self, message: Message) -> None:
        """Publish a message to NATS JetStream.

        Args:
            message: Message to store.
        """
        if not self._connected:
            self.connect()

        subject = self._subject(message.channel)
        payload = self._msg_to_nats_payload(message)

        async def _pub() -> None:
            assert self._js is not None
            await self._js.publish(subject, payload)

        self._run_async(_pub())

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
    ) -> list[Message]:
        """Retrieve messages from a channel via JetStream.

        Creates an ephemeral ordered consumer, fetches messages,
        and filters by the ``after`` cursor.

        Args:
            channel: Channel to query.
            after: If provided, only return messages with ID > this value.
            limit: Maximum number of messages to return.

        Returns:
            Messages in chronological order (oldest first).
        """
        if not self._connected:
            self.connect()

        subject = self._subject(channel)

        async def _query() -> list[Message]:
            assert self._js is not None
            messages: list[Message] = []

            try:
                sub = await self._js.subscribe(
                    subject,
                    ordered_consumer=True,
                )
                # Fetch up to a reasonable batch; we filter client-side
                fetch_limit = limit * 2 if after else limit
                try:
                    while len(messages) < limit:
                        try:
                            msg = await asyncio.wait_for(
                                sub.next_msg(),
                                timeout=1.0,
                            )
                            piazza_msg = self._nats_payload_to_msg(msg.data)
                            if after and piazza_msg.id <= after:
                                continue
                            messages.append(piazza_msg)
                        except asyncio.TimeoutError:
                            break
                finally:
                    await sub.unsubscribe()
            except Exception:
                pass

            return messages

        return self._run_async(_query())

    def list_channels(self) -> list[str]:
        """List all channels by querying JetStream stream subjects.

        Returns:
            Sorted list of channel names.
        """
        if not self._connected:
            self.connect()

        async def _list() -> list[str]:
            assert self._js is not None
            channels: set[str] = set()

            try:
                # Get stream info to find subjects with messages
                info = await self._js.find_stream_info_by_subject(
                    f"{self._subject_prefix}.>"
                )
                # Scan messages to find active channels
                sub = await self._js.subscribe(
                    f"{self._subject_prefix}.>",
                    ordered_consumer=True,
                )
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(
                                sub.next_msg(),
                                timeout=1.0,
                            )
                            piazza_msg = self._nats_payload_to_msg(msg.data)
                            channels.add(piazza_msg.channel)
                        except asyncio.TimeoutError:
                            break
                finally:
                    await sub.unsubscribe()
            except Exception:
                pass

            return sorted(channels)

        return self._run_async(_list())

    def count_messages(self, channel: str | None = None) -> int:
        """Count messages, optionally filtered by channel.

        Args:
            channel: If provided, count only messages in this channel.

        Returns:
            Number of messages.
        """
        if not self._connected:
            self.connect()

        async def _count() -> int:
            assert self._js is not None
            count = 0

            subject = (
                self._subject(channel) if channel else f"{self._subject_prefix}.>"
            )
            try:
                sub = await self._js.subscribe(subject, ordered_consumer=True)
                try:
                    while True:
                        try:
                            await asyncio.wait_for(sub.next_msg(), timeout=1.0)
                            count += 1
                        except asyncio.TimeoutError:
                            break
                finally:
                    await sub.unsubscribe()
            except Exception:
                pass

            return count

        return self._run_async(_count())

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
        if not self._connected:
            self.connect()

        subject = (
            self._subject(channel) if channel else f"{self._subject_prefix}.>"
        )

        async def _query_all() -> list[Message]:
            assert self._js is not None
            messages: list[Message] = []

            try:
                sub = await self._js.subscribe(subject, ordered_consumer=True)
                try:
                    while len(messages) < limit:
                        try:
                            msg = await asyncio.wait_for(
                                sub.next_msg(),
                                timeout=1.0,
                            )
                            piazza_msg = self._nats_payload_to_msg(msg.data)
                            if after and piazza_msg.id <= after:
                                continue
                            if sender and piazza_msg.sender != sender:
                                continue
                            if msg_type and piazza_msg.msg_type != msg_type:
                                continue
                            messages.append(piazza_msg)
                        except asyncio.TimeoutError:
                            break
                finally:
                    await sub.unsubscribe()
            except Exception:
                pass

            return messages

        return self._run_async(_query_all())

    def get_stats(self) -> dict:
        """Return aggregate statistics.

        Returns:
            Dict with total_messages, total_channels, total_senders,
            channel_breakdown, and msg_type_distribution.
        """
        if not self._connected:
            self.connect()

        async def _stats() -> dict:
            assert self._js is not None

            all_msgs: list[Message] = []
            try:
                sub = await self._js.subscribe(
                    f"{self._subject_prefix}.>",
                    ordered_consumer=True,
                )
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(
                                sub.next_msg(),
                                timeout=1.0,
                            )
                            all_msgs.append(self._nats_payload_to_msg(msg.data))
                        except asyncio.TimeoutError:
                            break
                finally:
                    await sub.unsubscribe()
            except Exception:
                pass

            senders: set[str] = set()
            types: dict[str, int] = {}
            breakdown: dict[str, dict] = {}
            channels: set[str] = set()

            for m in all_msgs:
                senders.add(m.sender)
                channels.add(m.channel)
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
                "total_channels": len(channels),
                "total_senders": len(senders),
                "channel_breakdown": sorted(
                    breakdown.values(),
                    key=lambda x: x["message_count"],
                    reverse=True,
                ),
                "msg_type_distribution": [
                    {"msg_type": t, "count": c}
                    for t, c in sorted(
                        types.items(), key=lambda x: x[1], reverse=True
                    )
                ],
            }

        return self._run_async(_stats())

    def query_recent_timestamps(self, seconds: int = 60) -> list[str]:
        """Return timestamps of messages from the last N seconds.

        Args:
            seconds: Time window in seconds.

        Returns:
            List of ISO 8601 timestamp strings, sorted ascending.
        """
        if not self._connected:
            self.connect()

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds)
        ).isoformat()

        async def _recent() -> list[str]:
            assert self._js is not None
            result: list[str] = []

            try:
                # Use deliver policy to start from recent time
                deliver_time = datetime.now(timezone.utc) - timedelta(
                    seconds=seconds
                )
                config = ConsumerConfig(
                    deliver_policy=DeliverPolicy.BY_START_TIME,
                    opt_start_time=deliver_time.isoformat(),
                )
                sub = await self._js.subscribe(
                    f"{self._subject_prefix}.>",
                    config=config,
                )
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(
                                sub.next_msg(),
                                timeout=1.0,
                            )
                            piazza_msg = self._nats_payload_to_msg(msg.data)
                            if piazza_msg.timestamp > cutoff:
                                result.append(piazza_msg.timestamp)
                        except asyncio.TimeoutError:
                            break
                finally:
                    await sub.unsubscribe()
            except Exception:
                # Fallback: scan all
                pass

            result.sort()
            return result

        return self._run_async(_recent())

    def get_backend_info(self) -> dict:
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
            try:
                stream_info = await self._js.find_stream_info_by_subject(
                    f"{self._subject_prefix}.>"
                )
                state = stream_info.state
                info_dict["total_messages"] = state.messages
                info_dict["total_bytes"] = state.bytes
                info_dict["total_channels"] = state.num_subjects
                info_dict["first_seq"] = state.first_seq
                info_dict["last_seq"] = state.last_seq
            except Exception:
                pass
            return info_dict

        return self._run_async(_info())

    def close(self) -> None:
        """Close NATS connection and stop background loop."""
        if self._nc is not None and self._connected:

            async def _close() -> None:
                assert self._nc is not None
                await self._nc.close()

            try:
                self._run_async(_close())
            except Exception:
                pass
            self._connected = False

        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread is not None:
                self._loop_thread.join(timeout=5)
            self._loop.close()
            self._loop = None
            self._loop_thread = None

    def __repr__(self) -> str:
        return f"NATSBackend({self._url!r}, stream={self._stream_name!r})"
