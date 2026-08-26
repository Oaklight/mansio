"""SQLite message backend for mansio bus."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from mansio._vendor.retry import retry
from mansio._vendor.structlog import get_logger
from mansio.protocols import Backend, ChannelStore, Compactable, Deletable, Presenceable
from mansio.types import (
    PERMISSION_LEVELS,
    ACLEntry,
    AgentPresence,
    ChannelMeta,
    ClaimResult,
    Message,
)

_CHANNEL_TYPE_PREFIXES: list[tuple[str, str]] = [
    ("dm:", "dm"),
    ("notebook:", "notebook"),
    ("memory:", "memory"),
    ("broadcast:", "broadcast"),
    ("_system:", "system"),
]


def _infer_channel_type(name: str) -> str:
    """Infer channel type from its name prefix."""
    for prefix, ctype in _CHANNEL_TYPE_PREFIXES:
        if name.startswith(prefix):
            return ctype
    return "user"


logger = get_logger(__name__)

# Current schema version — bump when the schema changes.
SCHEMA_VERSION = 2

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
CREATE TABLE IF NOT EXISTS channels (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'public',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_acl (
    channel TEXT NOT NULL REFERENCES channels(name) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    permission TEXT NOT NULL DEFAULT 'write',
    granted_at TEXT NOT NULL,
    granted_by TEXT,
    PRIMARY KEY (channel, agent_id)
);
"""

_META_TABLE = """\
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SchemaVersionError(Exception):
    """Raised when the database schema version is incompatible."""


def _get_schema_version(conn: sqlite3.Connection) -> int | None:
    """Read the schema version from the _meta table.

    Returns:
        The schema version integer, or None if the _meta table
        does not exist or has no schema_version row.
    """
    try:
        row = conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        # _meta table doesn't exist
        return None
    return int(row[0]) if row else None


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Write the schema version to the _meta table."""
    conn.executescript(_META_TABLE)
    conn.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Check if a table exists in the database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def backup_database(db_path: str | Path) -> Path | None:
    """Create a timestamped backup of the database file.

    Args:
        db_path: Path to the database file.

    Returns:
        Path to the backup file, or None if the source doesn't exist
        or is an in-memory database.
    """
    db_path = Path(db_path)
    if not db_path.exists() or str(db_path) == ":memory:":
        return None
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_suffix(f".db.bak.{ts}")
    shutil.copy2(str(db_path), str(backup_path))
    logger.info("Database backed up", source=str(db_path), backup=str(backup_path))
    return backup_path


def check_schema(
    conn: sqlite3.Connection,
    db_path: str,
    *,
    force_init: bool = False,
) -> str:
    """Check database schema version and decide on action.

    Args:
        conn: Open SQLite connection.
        db_path: Database file path (for logging / backup).
        force_init: If True, allow overwriting unknown schemas.

    Returns:
        One of: "fresh", "current", "legacy", "migrated".

    Raises:
        SchemaVersionError: If the schema is from a newer version
            or is unrecognized (without force_init).
    """
    stored = _get_schema_version(conn)
    has_messages = _has_table(conn, "messages")

    if stored is None and not has_messages:
        # Fresh database — no tables at all
        return "fresh"

    if stored is None and has_messages:
        # Legacy database — has data but no version tracking.
        # Stamp it with the current version (v1 is the original schema).
        _set_schema_version(conn, SCHEMA_VERSION)
        conn.commit()
        logger.info(
            "Legacy database detected, stamped with schema version",
            version=SCHEMA_VERSION,
            db=db_path,
        )
        return "legacy"

    if stored == SCHEMA_VERSION:
        return "current"

    # At this point stored is guaranteed to be int (None cases returned above)
    assert stored is not None

    if stored > SCHEMA_VERSION:
        if force_init:
            logger.warning(
                "Overriding future schema version (MANSIO_FORCE_INIT=1)",
                stored_version=stored,
                expected_version=SCHEMA_VERSION,
                db=db_path,
            )
            _set_schema_version(conn, SCHEMA_VERSION)
            conn.commit()
            return "current"
        raise SchemaVersionError(
            f"Database '{db_path}' has schema version {stored}, but this "
            f"build only supports version {SCHEMA_VERSION}. Upgrade mansio "
            f"or restore from backup."
        )

    # stored < SCHEMA_VERSION — migration needed.
    # For now there are no migrations (v1 is the first and only version),
    # but the framework is ready. Future versions add migration functions
    # to _MIGRATIONS and they run sequentially.
    if str(db_path) != ":memory:":
        backup_database(db_path)

    # Run migrations from stored+1 to SCHEMA_VERSION
    for target_ver in range(stored + 1, SCHEMA_VERSION + 1):
        migration_fn = _MIGRATIONS.get(target_ver)
        if migration_fn is None:
            raise SchemaVersionError(
                f"No migration path from schema version {stored} to "
                f"{SCHEMA_VERSION}. Back up your data and reinstall."
            )
        logger.info("Running migration", from_version=target_ver - 1, to_version=target_ver)
        migration_fn(conn)

    _set_schema_version(conn, SCHEMA_VERSION)
    conn.commit()
    logger.info("Schema migration complete", version=SCHEMA_VERSION, db=db_path)
    return "migrated"


# ── Migration registry ──────────────────────────────────────────────


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add channels and channel_acl tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            name     TEXT PRIMARY KEY,
            owner    TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'public',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS channel_acl (
            channel    TEXT NOT NULL REFERENCES channels(name) ON DELETE CASCADE,
            agent_id   TEXT NOT NULL,
            permission TEXT NOT NULL DEFAULT 'read',
            granted_at TEXT NOT NULL DEFAULT '',
            granted_by TEXT,
            PRIMARY KEY (channel, agent_id)
        );
    """)


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v1_to_v2,
}


class SQLiteBackend(Backend, Presenceable, Compactable, Deletable, ChannelStore):
    """SQLite-backed message backend.

    Supports cross-process sharing via WAL mode.

    Args:
        db_path: Path to SQLite database file. Use ":memory:" for
            ephemeral storage (testing).
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        force_init: bool = False,
    ) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Set busy_timeout BEFORE any other PRAGMA so subsequent
        # statements wait for locks instead of failing immediately.
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Switching to WAL journal_mode requires an exclusive lock.
        # SQLite's busy handler does NOT cover PRAGMA journal_mode
        # mutations — the lock contention surfaces as an immediate
        # OperationalError("database is locked"). Retry with backoff
        # so multiple processes can cold-start the same DB without
        # racing each other.
        self._enable_wal()

        # Schema version check — may backup and migrate, or abort.
        result = check_schema(self._conn, self._db_path, force_init=force_init)
        if result == "fresh":
            logger.info("Initializing fresh database", db=self._db_path)
        elif result == "legacy":
            logger.info("Existing database adopted", db=self._db_path)

        self._conn.executescript(_SCHEMA)
        self._ensure_queue_columns()
        self._ensure_threading_columns()
        if result == "fresh":
            _set_schema_version(self._conn, SCHEMA_VERSION)
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
        self._conn.execute("PRAGMA foreign_keys=ON")

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

    def _ensure_threading_columns(self) -> None:
        """Idempotent migration: add parent_id and thread_id columns."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
        for col in ("parent_id", "thread_id"):
            if col not in existing:
                with contextlib.suppress(sqlite3.OperationalError):
                    self._conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_thread ON messages (channel, thread_id)"
        )

    def store(self, message: Message) -> None:
        """Persist a regular message to SQLite.

        Args:
            message: Message to store.
        """
        meta_json = json.dumps(message.metadata) if message.metadata else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages "
                "(id, channel, sender, msg_type, payload, timestamp, metadata, status, "
                "parent_id, thread_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.channel,
                    message.sender,
                    message.msg_type,
                    message.payload,
                    message.timestamp,
                    meta_json,
                    None,
                    message.parent_id,
                    message.thread_id,
                ),
            )
            self._conn.commit()

    def store_queue(self, message: Message) -> None:
        """Persist a message as a claimable queue item.

        Args:
            message: Message to store as a queue item.
        """
        meta_json = json.dumps(message.metadata) if message.metadata else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages "
                "(id, channel, sender, msg_type, payload, timestamp, metadata, status, "
                "parent_id, thread_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.channel,
                    message.sender,
                    message.msg_type,
                    message.payload,
                    message.timestamp,
                    meta_json,
                    "unclaimed",
                    message.parent_id,
                    message.thread_id,
                ),
            )
            self._conn.commit()

    def get_message(self, message_id: str) -> Message | None:
        """Retrieve a single message by ID."""
        with self._lock:
            cursor = self._conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
            row = cursor.fetchone()
            return self._row_to_message(row) if row else None

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
        msg_type: str | None = None,
        order: Literal["oldest", "newest"] = "oldest",
        thread_id: str | None = None,
        offset: int = 0,
    ) -> list[Message]:
        """Retrieve messages from a channel.

        Args:
            channel: Channel to query.
            after: If provided, only return messages with ID > this value.
            limit: Maximum number of messages to return.
            msg_type: If provided, only return messages of this type.
            order: ``"oldest"`` returns the first *limit* messages;
                ``"newest"`` returns the last *limit* messages.  Both
                return results in chronological (ascending ID) order.
            thread_id: If provided, only return messages in this thread.
            offset: Number of messages to skip before returning results.

        Returns:
            Messages in chronological order (oldest first).
        """
        with self._lock:
            clauses = ["channel = ?"]
            params: list = [channel]
            if after:
                clauses.append("id > ?")
                params.append(after)
            if msg_type is not None:
                clauses.append("msg_type = ?")
                params.append(msg_type)
            if thread_id is not None:
                clauses.append("thread_id = ?")
                params.append(thread_id)
            where = " AND ".join(clauses)
            if order == "newest":
                # For newest+offset: skip `offset` most-recent rows,
                # then return next `limit` in chronological order.
                params.append(limit)
                params.append(offset)
                sql = (
                    f"SELECT * FROM ("
                    f"SELECT * FROM messages WHERE {where} "
                    f"ORDER BY id DESC LIMIT ? OFFSET ?"
                    f") sub ORDER BY id ASC"
                )
            else:
                params.append(limit)
                if offset:
                    sql = f"SELECT * FROM messages WHERE {where} ORDER BY id ASC LIMIT ? OFFSET ?"
                    params.append(offset)
                else:
                    sql = f"SELECT * FROM messages WHERE {where} ORDER BY id ASC LIMIT ?"
            cursor = self._conn.execute(sql, params)
            return [self._row_to_message(row) for row in cursor.fetchall()]

    def list_channels_detail(self) -> list[dict]:
        """List all channels with metadata.

        Returns:
            List of dicts with keys: name, message_count, last_activity,
            sender_count, type.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT channel, COUNT(*) as message_count, "
                "MAX(timestamp) as last_activity, "
                "COUNT(DISTINCT sender) as sender_count "
                "FROM messages GROUP BY channel ORDER BY channel"
            )
            return [
                {
                    "name": row["channel"],
                    "message_count": row["message_count"],
                    "last_activity": row["last_activity"],
                    "sender_count": row["sender_count"],
                    "type": _infer_channel_type(row["channel"]),
                }
                for row in cursor.fetchall()
            ]

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

    def message_count(self, channel: str | None = None) -> int:
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
        clauses: list[str] = []
        params: list = []
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if sender:
            clauses.append("sender = ?")
            params.append(sender)
        if msg_type is not None:
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

    def stats(self) -> dict:
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

    def recent_timestamps(self, seconds: int = 60) -> list[str]:
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
        # parent_id/thread_id may not exist in old DBs before migration runs
        try:
            parent_id = row["parent_id"]
        except (IndexError, KeyError):
            parent_id = None
        try:
            thread_id = row["thread_id"]
        except (IndexError, KeyError):
            thread_id = None
        return Message(
            id=row["id"],
            channel=row["channel"],
            sender=row["sender"],
            msg_type=row["msg_type"],
            payload=row["payload"],
            timestamp=row["timestamp"],
            metadata=metadata,
            parent_id=parent_id or None,
            thread_id=thread_id or None,
        )

    def queue_claim(
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
                lease_until=lease_until,
            )

    def queue_ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
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

    def queue_status(self, message_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, claimed_by, claimed_at, lease_until "
                "FROM messages WHERE id = ? AND status IS NOT NULL",
                (message_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "status": row["status"],
                "claimed_by": row["claimed_by"],
                "claimed_at": row["claimed_at"],
                "lease_until": row["lease_until"],
            }

    def queue_stats(self, channel: str | None = None) -> dict:
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

    def queue_retire(self, max_age_seconds: int = 86400, max_per_channel: int = 1000) -> int:
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

    # ── Deletion ─────────────────────────────────────────────

    def delete_channel(self, channel: str) -> int:
        """Delete a channel and all its messages.

        Args:
            channel: Channel name to delete.

        Returns:
            Number of messages deleted.
        """
        with self._lock:
            cursor = self._conn.execute("DELETE FROM messages WHERE channel = ?", (channel,))
            self._conn.commit()
            return cursor.rowcount

    def delete_message(self, message_id: str) -> bool:
        """Delete a single message by ID.

        Args:
            message_id: ID of the message to delete.

        Returns:
            True if the message was found and deleted, False otherwise.
        """
        with self._lock:
            cursor = self._conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def info(self) -> dict:
        """Return backend type, config, and usage info."""
        info_dict: dict = {
            "type": "sqlite",
            "db_path": self._db_path,
            "journal_mode": "WAL",
        }
        with self._lock:
            info_dict["total_messages"] = self._conn.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            info_dict["total_channels"] = self._conn.execute(
                "SELECT COUNT(DISTINCT channel) FROM messages"
            ).fetchone()[0]
            page_count = self._conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
            info_dict["db_size_bytes"] = page_count * page_size
            info_dict["db_size_mb"] = round(page_count * page_size / (1024 * 1024), 2)
            freelist = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
            info_dict["freelist_pages"] = freelist

        if self._db_path != ":memory:" and os.path.exists(self._db_path):
            info_dict["file_size_bytes"] = os.path.getsize(self._db_path)
            info_dict["file_size_mb"] = round(os.path.getsize(self._db_path) / (1024 * 1024), 2)
            wal_path = self._db_path + "-wal"
            if os.path.exists(wal_path):
                info_dict["wal_size_bytes"] = os.path.getsize(wal_path)
                info_dict["wal_size_mb"] = round(os.path.getsize(wal_path) / (1024 * 1024), 2)

        return info_dict

    # ── Channel metadata & ACL ────────────────────────────────

    def create_channel(self, meta: ChannelMeta, acl: list[ACLEntry] | None = None) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO channels (name, owner, visibility, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (meta.name, meta.owner, meta.visibility, meta.created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Channel '{meta.name}' already exists") from exc
            if acl:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO channel_acl "
                    "(channel, agent_id, permission, granted_at, granted_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (e.channel, e.agent_id, e.permission, e.granted_at, e.granted_by)
                        for e in acl
                    ],
                )
            self._conn.commit()

    def get_channel(self, name: str) -> ChannelMeta | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name, owner, visibility, created_at FROM channels WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                return None
            return ChannelMeta(name=row[0], owner=row[1], visibility=row[2], created_at=row[3])

    def list_channels_meta(self) -> list[ChannelMeta]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, owner, visibility, created_at FROM channels ORDER BY name"
            ).fetchall()
            return [
                ChannelMeta(name=r[0], owner=r[1], visibility=r[2], created_at=r[3]) for r in rows
            ]

    def update_channel(
        self, name: str, *, visibility: str | None = None, owner: str | None = None
    ) -> bool:
        with self._lock:
            sets: list[str] = []
            params: list[str] = []
            if visibility is not None:
                sets.append("visibility = ?")
                params.append(visibility)
            if owner is not None:
                sets.append("owner = ?")
                params.append(owner)
            if not sets:
                return self.get_channel(name) is not None
            params.append(name)
            cursor = self._conn.execute(
                f"UPDATE channels SET {', '.join(sets)} WHERE name = ?", params
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_channel_meta(self, name: str) -> bool:
        with self._lock:
            # ACL entries cascade-deleted via FK
            cursor = self._conn.execute("DELETE FROM channels WHERE name = ?", (name,))
            self._conn.commit()
            return cursor.rowcount > 0

    def set_acl(self, channel: str, entries: list[ACLEntry]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM channel_acl WHERE channel = ?", (channel,))
            if entries:
                self._conn.executemany(
                    "INSERT INTO channel_acl "
                    "(channel, agent_id, permission, granted_at, granted_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (e.channel, e.agent_id, e.permission, e.granted_at, e.granted_by)
                        for e in entries
                    ],
                )
            self._conn.commit()

    def get_acl(self, channel: str) -> list[ACLEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, agent_id, permission, granted_at, granted_by "
                "FROM channel_acl WHERE channel = ? ORDER BY agent_id",
                (channel,),
            ).fetchall()
            return [
                ACLEntry(
                    channel=r[0],
                    agent_id=r[1],
                    permission=r[2],
                    granted_at=r[3],
                    granted_by=r[4],
                )
                for r in rows
            ]

    def add_acl_entry(self, entry: ACLEntry) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO channel_acl "
                "(channel, agent_id, permission, granted_at, granted_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entry.channel,
                    entry.agent_id,
                    entry.permission,
                    entry.granted_at,
                    entry.granted_by,
                ),
            )
            self._conn.commit()

    def remove_acl_entry(self, channel: str, agent_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM channel_acl WHERE channel = ? AND agent_id = ?",
                (channel, agent_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def check_access(self, channel: str, agent_id: str, required: str = "read") -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT owner, visibility FROM channels WHERE name = ?", (channel,)
            ).fetchone()
            if row is None:
                return True  # unregistered channels are public
            ch_owner, ch_vis = row[0], row[1]
            if ch_owner == agent_id:
                return True
            if ch_vis == "public" and required in ("read", "write"):
                return True
            acl_row = self._conn.execute(
                "SELECT permission FROM channel_acl WHERE channel = ? AND agent_id = ?",
                (channel, agent_id),
            ).fetchone()
            if acl_row is None:
                return False
            return PERMISSION_LEVELS.get(acl_row[0], 0) >= PERMISSION_LEVELS.get(required, 0)

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
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent_id, last_seen, metadata FROM agent_presence ORDER BY agent_id"
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

    def agent_status(self, agent_id: str, timeout_seconds: int = 120) -> AgentPresence | None:
        """Return presence for a single agent, or ``None`` if unknown."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT agent_id, last_seen, metadata FROM agent_presence WHERE agent_id = ?",
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
            total_removed = 0

            if keep_latest_per_sender:
                cursor = self._conn.execute(
                    """
                    DELETE FROM messages
                    WHERE channel = ? AND id NOT IN (
                        SELECT id FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY sender ORDER BY id DESC
                            ) AS rn
                            FROM messages WHERE channel = ?
                        ) WHERE rn = 1
                    )
                    """,
                    (channel, channel),
                )
                total_removed += cursor.rowcount

            if max_messages is not None:
                cursor = self._conn.execute(
                    """
                    DELETE FROM messages
                    WHERE channel = ? AND id NOT IN (
                        SELECT id FROM messages
                        WHERE channel = ?
                        ORDER BY id DESC
                        LIMIT ?
                    )
                    """,
                    (channel, channel, max_messages),
                )
                total_removed += cursor.rowcount

            if total_removed:
                self._conn.commit()

            return total_removed

    def __repr__(self) -> str:
        return f"SQLiteBackend({self._db_path!r})"
