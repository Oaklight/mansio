# mansio MVP — Implementation Reference

**Date:** 2026-04-20 (last updated 2026-06-11)
**Status:** ✅ Implemented

---

## Overview

mansio MVP provides a minimal, zero-dependency message bus for multi-agent context synchronization. All core layers (Backend → Bus → Client SDK) are implemented and tested.

### What's Included

| Layer | Component | Status |
|-------|-----------|--------|
| **Backend** | `SQLiteBackend` — WAL-mode, cross-process, single-file persistence | ✅ |
| **Backend** | `MemoryBackend` — pure in-memory, for testing | ✅ |
| **Bus** | `Bus` — orchestrator combining Backend + Serializer + in-process pub/sub | ✅ |
| **Bus** | `SQLiteBus` — convenience shorthand for `Bus(backend=SQLiteBackend(...))` | ✅ |
| **Serializer** | `JSONSerializer` — human-readable metadata encoding | ✅ |
| **Client SDK** | `MansioClient` — identity, cursor, channel naming, semantic API | ✅ |
| **Admin** | Admin panel — modular HTTP dashboard with route-based handlers | ✅ |

### What's Deferred

- Hub-Server architecture (MansioServer + RemoteTransport)
- IRC Frontend (expose Bus via IRC protocol for human observation)
- Additional Backends (NATS JetStream, Redis Streams, etc.)
- Typed channel enforcement at Bus layer
- Moderator mechanism
- Message TTL / retention signals
- Priority queue / delayed messages
- Async API
- MCP/REST/CLI Delivery layer
- Federation (cross-instance communication, see Issue #4)
- Secret rotation / agent revocation

---

## Quick Start

```python
from mansio import Bus, MemoryBackend, MansioClient

# In-process: agents share a Bus
bus = Bus(backend=MemoryBackend())
alice = MansioClient(bus, "alice-agent")
bob = MansioClient(bus, "bob-agent")

# Send a DM
alice.dm_send("bob-agent", "hello!")
messages = bob.dm_read("alice-agent")

# Notes and thoughts
alice.note_write("design decision: use SQLite", tags=["architecture"])
alice.thought_record("reasoning", "backend choice", "SQLite has zero deps...")

# Memory
alice.memory_store("user prefers dark mode", memory_type="preference")
results = alice.memory_recall("dark mode")

# Cleanup
alice.close()
bob.close()
bus.close()
```

### Connection Targets

```python
# Bus object (in-process, caller manages lifecycle)
client = MansioClient(bus, "agent-1")

# :memory: (auto-creates Bus, client owns it)
client = MansioClient(":memory:", "agent-1")

# File path (SQLite persistence, client owns Bus)
client = MansioClient("workspace/.mansio.db", "agent-1")

# Future: redis://, amqp://, http:// → NotImplementedError
```

---

## Architecture

```
┌──────────────────────────────────────────────┐
│          MansioClient (Client SDK)           │
│  Identity · Cursor · Channel naming · API    │
├──────────────────────────────────────────────┤
│         Transport (LocalTransport)           │
├──────────────────────────────────────────────┤
│               Bus (Orchestrator)             │
│    Backend + Serializer + in-process pub/sub │
├──────────────────────────────────────────────┤
│    SQLiteBackend  │  MemoryBackend           │
│    (WAL mode)     │  (testing)               │
└──────────────────────────────────────────────┘
```

### Admin Panel Structure

The admin panel uses a modular route architecture (since PR #17):

```
admin/
├── handlers.py          # thin dispatcher (~80 lines)
├── server.py            # AdminServer lifecycle
├── auth.py              # token authentication
├── static.py            # embedded HTML
└── routes/
    ├── _shared.py       # response helpers (JSON, CORS, error)
    ├── ui.py            # root path serving
    ├── dashboard.py     # stats + throughput
    ├── channels.py      # channel list + detail
    ├── messages.py      # message query + publish
    └── subscriptions.py # subscription listing
```

---

## API Reference

### Message

```python
@dataclass(frozen=True)
class Message:
    id: str              # UUID v7 (time-ordered)
    channel: str         # channel name
    sender: str          # agent ID
    msg_type: str        # "chat" | "note" | "thought" | "memory" | ...
    payload: str         # content (JSON string or plain text)
    timestamp: str       # ISO 8601
    metadata: dict | None  # optional extra fields
```

### Bus

```python
class Bus:
    def __init__(self, backend=None, serializer=None, *, require_auth=False)
    def publish(channel, sender, msg_type, payload, metadata=None) -> str
    def poll(channel, after=None, limit=100) -> list[Message]
    def subscribe(channel, callback) -> str
    def unsubscribe(subscription_id) -> None
    def channels() -> list[str]
    def close() -> None
    # Context manager: with Bus(...) as bus: ...
```

### MansioClient

#### Constructor & Lifecycle

```python
class MansioClient:
    def __init__(target: Bus | str, agent_id: str, *,
                 secret=None, display_name=None)

    @classmethod
    def register(target, agent_id, *, display_name=None)
        -> tuple[MansioClient, str]

    def close() -> None
    # Context manager: with MansioClient(...) as client: ...
```

#### Core API — Channel Operations

```python
    # Send message to channel
    def channel_send(channel, content, msg_type="chat",
                     metadata=None) -> str

    # Read messages (no cursor advancement)
    def channel_read(channel, limit=10, after=None) -> list[Message]

    # Poll for new messages (cursor auto-advances)
    def channel_poll(channel) -> list[Message]

    # List all channels
    def channel_list() -> list[str]
```

#### Semantic API — Sugar Methods

```python
    # Notes (writes to notebook:{agent_id})
    def note_write(content, tags=None) -> str
    def note_read(tags=None, limit=10) -> list[Message]

    # Thoughts (writes to notebook:{agent_id}, msg_type="thought")
    def thought_record(thinking_mode, focus_area, thought_process) -> str
    def thought_read(limit=10) -> list[Message]

    # Memory (writes to memory:{agent_id})
    def memory_store(content, memory_type="general") -> str
    def memory_recall(query, limit=5) -> list[Message]

    # DM (writes to dm:{sorted_pair})
    def dm_send(to_agent, content) -> str
    def dm_read(with_agent, limit=10) -> list[Message]

    # Broadcast (reads from broadcast:{topic})
    def broadcast_list() -> list[str]
    def broadcast_read(topic, limit=10) -> list[Message]

    # Notifications
    def notification_check() -> list[Message]
```

---

## Identity & Authentication

### Agent ID

Format: `^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$` (3-64 chars, lowercase alphanumeric + hyphens).

### Authentication Modes

```python
# No auth (default) — development / testing
bus = Bus(require_auth=False)
client = MansioClient(bus, "agent-1")  # just works

# Auth required — production / shared environments
bus = Bus(require_auth=True)
client, secret = MansioClient.register(bus, "agent-1")
# Store secret, reconnect later:
client = MansioClient(bus, "agent-1", secret=secret)
```

- Secrets are `sk-{48 hex chars}`, stored as `sha256:{hash}` in `_system:registry`
- Registration via `MansioClient.register()` returns `(client, secret)` tuple
- Cross-session reconnect: same `agent_id` + `secret`

---

## Channel Naming Conventions

Enforced by Client SDK (semantic API methods), not by Bus.

| Type | Pattern | Used By |
|------|---------|---------|
| Notebook | `notebook:{agent_id}` | `note_write`, `thought_record` |
| Memory | `memory:{agent_id}` | `memory_store`, `memory_recall` |
| DM | `dm:{agent_a}:{agent_b}` (sorted) | `dm_send`, `dm_read` |
| Broadcast | `broadcast:{topic}` | `broadcast_list`, `broadcast_read` |
| System | `_system:registry` | Agent registration |
| System | `_system:agents` | Presence announcement |
| System | `_system:cursors:{agent_id}` | Cursor persistence |
| System | `_system:notifications:{agent_id}` | `notification_check` |

---

## Cursor Management

Two read modes:

| Method | Cursor | Use Case |
|--------|--------|----------|
| `channel_poll(ch)` | ✅ Auto-advances | Track new messages incrementally |
| `channel_read(ch)` | ❌ No effect | Random access, view history |

Cursors persist across sessions via `_system:cursors:{agent_id}`:
- `close()` saves cursor snapshot
- New `MansioClient` with same `agent_id` restores from latest snapshot
- `channel_poll()` resumes from where the previous session left off

---

## Schema (SQLite)

```sql
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata TEXT  -- JSON or NULL
);

CREATE INDEX IF NOT EXISTS idx_channel_id ON messages (channel, id);
CREATE INDEX IF NOT EXISTS idx_channel_ts ON messages (channel, timestamp);
```

---

## Cross-Process Usage

Multiple processes share the same SQLite file via WAL mode:

```python
# Process A
client_a = MansioClient("shared/.mansio.db", "coder-1")
client_a.channel_send("sync", '{"commits": ["abc"]}', msg_type="context_sync")

# Process B
client_b = MansioClient("shared/.mansio.db", "reviewer-1")
new_msgs = client_b.channel_poll("sync")
```

- `subscribe()` callbacks only fire within the same process
- Cross-process uses `channel_poll()` for message discovery

---

## Design Decisions

1. **SQLite backend** — Zero external deps. WAL mode enables concurrent cross-process access.
2. **Message ID as cursor** — `poll(after=id)` uses UUID v7 (time-ordered), avoids clock skew.
3. **Sync API only** — Async doubles the surface for no MVP benefit.
4. **Poll + Subscribe** — Poll works cross-process; subscribe works in-process. Both simple.
5. **Channel naming at SDK layer** — Bus stays generic; MansioClient enforces conventions.
6. **Everything is messages** — Registry, cursors, and presence all stored as messages in `_system:` channels.
7. **`register()` uses `object.__new__()`** — Bypasses `__init__` auth to write registration before authentication can run.
8. **Transport abstraction** — Decouples MansioClient from in-process vs. network Bus access.

---

## Test Coverage

185 tests across 3 test files:

| File | Tests | Coverage |
|------|-------|----------|
| `test_bus.py` | Message, Serializer, Backends, Bus, SQLiteBus | 44 |
| `test_client.py` | Transport, Identity, Constructor, Lifecycle, Core API, Sugar API, Cursor, Auth | 87 |
| `test_admin.py` | Backend extensions, Auth, Admin Server, Admin API, Route modules | 54 |

All functions pass complexipy (max complexity ≤ 15).
