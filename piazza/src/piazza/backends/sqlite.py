"""SQLite message backend for piazza bus."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from piazza._vendor.retry import retry
from piazza._vendor.structlog import get_logger
from piazza.types import AgentPresence, ClaimResult, Message

logger = get_logger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_channel_id ON messages (channel, id);
CREATE INDEX IF NOT EXISTS idx_channel_ts ON messages (channel, timestamp);
CREATE TABLE IF NOT EXISTS agent_presence (
    agent_id TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL,
    metadata TEXT
);
"""


class SQLiteBackend:
    """SQLite-backed message backend.

    Supports cross-process sharing via WAL mode.

    Args:
        db_path: Path to SQLite database file. Use ":memory:" for
            ephemeral storage (testing).
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Set busy_timeout BEFORE any other PRAGMA so subsequent
        # statements wait for locks instead of failing immediately.
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Switching to WAL journal_mode requires an exclusive lock.
        # SQLite's busy handler does NOT cover PRAGMA journal_mode
        # mutations -- the lock contention surfaces as an immediate
        # OperationalError("database is locked"). Retry with backoff
        # so multiple processes can cold-start the same DB without
        # racing each other.
        self._enable_wal()
        self._conn.executescript(_SCHEMA)
        self._ensure_queue_columns()
        self._conn.commit()

    @retry(
        max_retries=50,
        base_delay=0.05,
        backoff="linear",
        retry_on=(sqlite3.OperationalError,),
        on_retry=lambda s: (
            logger.warning("WAL journal_mode switch locked, retrying", attempt=s.attempt)
            if s.attempt % 10 == 0
            else None
        ),
    )
    def _enable_wal(self) -> None:
        """Enable WAL journal mode with automatic retry on lock contention."""
        self._conn.execute("PRAGMA journal_mode=WAL")

    def _ensure_queue_columns(self) -> None:
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
        for col, col_type in [
            ("status", "TEXT"),
            ("claimed_by", "TEXT"),
            ("claimed_at", "TEXT"),
            ("lease_until", "TEXT"),
        ]:
            if col not in existing:
                with contextlib.suppress(sqlite3.OperationalError):
                    self._conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {col_type}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_status ON messages (channel, status, id)"
        )

    def store(self, message: Message, *, queue: bool = False) -> None:
        """Persist a message to SQLite.

        Args:
            message: Message to store.
        """
        meta_json = json.dumps(message.metadata) if message.metadata else None
        status = "unclaimed" if queue else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages "
                "(id, channel, sender, msg_type, payload, timestamp, metadata, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.channel,
                    message.sender,
                    message.msg_type,
                    message.payload,
                    message.timestamp,
                    meta_json,
                    status,
                ),
            )
            self._conn.commit()

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
            if after:
                cursor = self._conn.execute(
                    "SELECT * FROM messages WHERE channel = ? AND id > ? ORDER BY id ASC LIMIT ?",
                    (channel, after, limit),
                )
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM messages WHERE channel = ? ORDER BY id ASC LIMIT ?",
                    (channel, limit),
                )
            return [self._row_to_message(row) for row in cursor.fetchall()]

    def list_channels(self) -> list[str]:
        """List all channels that have at least one message.

        Returns:
            Sorted list of channel names.
        """
        with self._lock:
            cursor = self._conn.execute("SELECT DISTINCT channel FROM messages ORDER BY channel")
            return [row["channel"] for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the SQLite database connection."""
        self._conn.close()

    def count_messages(self, channel: str | None = None) -> int:
        """Count messages, optionally filtered by channel.

        Args:
            channel: If provided, count only messages in this channel.

        Returns:
            Number of messages.
        """
        with self._lock:
            if channel:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE channel = ?", (channel,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            return row[0]

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
        clauses: list[str] = []
        params: list = []
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if sender:
            clauses.append("sender = ?")
            params.append(sender)
        if msg_type:
            clauses.append("msg_type = ?")
            params.append(msg_type)
        if after:
            clauses.append("id > ?")
            params.append(after)

        where = " AND ".join(clauses) if clauses else "1=1"
        params.append(limit)
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY id ASC LIMIT ?",
                params,
            )
            return [self._row_to_message(row) for row in cursor.fetchall()]

    def get_stats(self) -> dict:
        """Return aggregate statistics for admin dashboard.

        Returns:
            Dict with total_messages, total_channels, total_senders,
            channel_breakdown, and msg_type_distribution.
        """
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            channels = self._conn.execute(
                "SELECT COUNT(DISTINCT channel) FROM messages"
            ).fetchone()[0]
            senders = self._conn.execute("SELECT COUNT(DISTINCT sender) FROM messages").fetchone()[
                0
            ]

            breakdown = []
            for row in self._conn.execute(
                "SELECT channel, COUNT(*) as cnt, MAX(timestamp) as last_ts, "
                "COUNT(DISTINCT sender) as scnt "
                "FROM messages GROUP BY channel ORDER BY cnt DESC"
            ).fetchall():
                breakdown.append(
                    {
                        "channel": row[0],
                        "message_count": row[1],
                        "last_message_time": row[2],
                        "sender_count": row[3],
                    }
                )

            types = []
            for row in self._conn.execute(
                "SELECT msg_type, COUNT(*) as cnt FROM messages GROUP BY msg_type ORDER BY cnt DESC"
            ).fetchall():
                types.append({"msg_type": row[0], "count": row[1]})

            return {
                "total_messages": total,
                "total_channels": channels,
                "total_senders": senders,
                "channel_breakdown": breakdown,
                "msg_type_distribution": types,
            }

    def query_recent_timestamps(self, seconds: int = 60) -> list[str]:
        """Return timestamps of messages from the last N seconds.

        Args:
            seconds: Time window in seconds.

        Returns:
            List of ISO 8601 timestamp strings, sorted ascending.
        """
        with self._lock:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
            cursor = self._conn.execute(
                "SELECT timestamp FROM messages WHERE timestamp > ? ORDER BY timestamp ASC",
                (cutoff,),
            )
            return [row[0] for row in cursor.fetchall()]

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        """Convert a database row to a Message instance.

        Args:
            row: SQLite row with message columns.

        Returns:
            Corresponding Message object.
        """
        meta_raw = row["metadata"]
        metadata = json.loads(meta_raw) if meta_raw else None
        return Message(
            id=row["id"],
            channel=row["channel"],
            sender=row["sender"],
            msg_type=row["msg_type"],
            payload=row["payload"],
            timestamp=row["timestamp"],
            metadata=metadata,
        )

    def claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        now = datetime.now(timezone.utc)
        claimed_at = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE messages "
                "SET status = 'claimed', claimed_by = ?, claimed_at = ?, lease_until = ? "
                "WHERE id = ("
                "  SELECT id FROM messages "
                "  WHERE channel = ? AND "
                "    (status = 'unclaimed' OR (status = 'claimed' AND lease_until < ?)) "
                "  ORDER BY id ASC LIMIT 1"
                ") RETURNING *",
                (claimed_by, claimed_at, lease_until, channel, claimed_at),
            )
            row = cursor.fetchone()
            self._conn.commit()
            if row is None:
                return None
            return ClaimResult(
                message=self._row_to_message(row),
                status="claimed",
                claimed_by=claimed_by,
                claimed_at=claimed_at,
            )

    def ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE messages SET status = 'completed' "
                "WHERE id = ? AND claimed_by = ? AND status = 'claimed' "
                "RETURNING *",
                (message_id, claimed_by),
            )
            row = cursor.fetchone()
            self._conn.commit()
            if row is None:
                return None
            return ClaimResult(
                message=self._row_to_message(row),
                status="completed",
                claimed_by=row["claimed_by"],
                claimed_at=row["claimed_at"],
            )

    def get_queue_stats(self, channel: str | None = None) -> dict:
        with self._lock:
            if channel:
                rows = self._conn.execute(
                    "SELECT status, COUNT(*) FROM messages "
                    "WHERE channel = ? AND status IS NOT NULL GROUP BY status",
                    (channel,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT status, COUNT(*) FROM messages "
                    "WHERE status IS NOT NULL GROUP BY status",
                ).fetchall()
        result = {"unclaimed": 0, "claimed": 0, "completed": 0}
        for row in rows:
            if row[0] in result:
                result[row[0]] = row[1]
        return result

    def retire_completed(self, max_age_seconds: int = 86400, max_per_channel: int = 1000) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE status = 'completed' AND claimed_at < ?",
                (cutoff,),
            )
            deleted = cur.rowcount
            channels = self._conn.execute(
                "SELECT DISTINCT channel FROM messages WHERE status = 'completed'"
            ).fetchall()
            for (ch,) in channels:
                cur2 = self._conn.execute(
                    "DELETE FROM messages WHERE status = 'completed' AND channel = ? "
                    "AND id NOT IN ("
                    "  SELECT id FROM messages "
                    "  WHERE status = 'completed' AND channel = ? "
                    "  ORDER BY id DESC LIMIT ?"
                    ")",
                    (ch, ch, max_per_channel),
                )
                deleted += cur2.rowcount
            self._conn.commit()
        return deleted

    def get_backend_info(self) -> dict:
        """Return backend type, config, and usage info."""
        import os

        info: dict = {
            "type": "sqlite",
            "db_path": self._db_path,
            "journal_mode": "WAL",
        }
        with self._lock:
            info["total_messages"] = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[
                0
            ]
            info["total_channels"] = self._conn.execute(
                "SELECT COUNT(DISTINCT channel) FROM messages"
            ).fetchone()[0]
            # SQLite page info
            page_count = self._conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
            info["db_size_bytes"] = page_count * page_size
            info["db_size_mb"] = round(page_count * page_size / (1024 * 1024), 2)
            freelist = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
            info["freelist_pages"] = freelist

        if self._db_path != ":memory:" and os.path.exists(self._db_path):
            info["file_size_bytes"] = os.path.getsize(self._db_path)
            info["file_size_mb"] = round(os.path.getsize(self._db_path) / (1024 * 1024), 2)
            wal_path = self._db_path + "-wal"
            if os.path.exists(wal_path):
                info["wal_size_bytes"] = os.path.getsize(wal_path)
                info["wal_size_mb"] = round(os.path.getsize(wal_path) / (1024 * 1024), 2)

        return info

    # ── Presence ──────────────────────────────────────────────

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*."""
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata) if metadata else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_presence (agent_id, last_seen, metadata) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET last_seen = excluded.last_seen, "
                "metadata = excluded.metadata",
                (agent_id, now, meta_json),
            )
            self._conn.commit()

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        """Return all known agents with computed online/offline status."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        ).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent_id, last_seen, metadata FROM agent_presence "
                "ORDER BY agent_id"
            ).fetchall()
        result: list[AgentPresence] = []
        for row in rows:
            status = "online" if row["last_seen"] >= cutoff else "offline"
            meta = json.loads(row["metadata"]) if row["metadata"] else None
            result.append(
                AgentPresence(
                    agent_id=row["agent_id"],
                    status=status,
                    last_seen=row["last_seen"],
                    metadata=meta,
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
        with self._lock:
            row = self._conn.execute(
                "SELECT agent_id, last_seen, metadata FROM agent_presence "
                "WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        status = "online" if row["last_seen"] >= cutoff else "offline"
        meta = json.loads(row["metadata"]) if row["metadata"] else None
        return AgentPresence(
            agent_id=row["agent_id"],
            status=status,
            last_seen=row["last_seen"],
            metadata=meta,
        )

    def __repr__(self) -> str:
        return f"SQLiteBackend({self._db_path!r})"
