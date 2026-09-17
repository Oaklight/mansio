# Agent Messaging Hub — Design Document

## 1. Project Overview

Mansio is a messaging backbone system designed for LLMs/Agents, providing unified communication infrastructure for multi-agent collaboration. This project serves as the reference implementation of the **Messaging** component (Chapter 9) in the PhD dissertation *"Enabling Agentic AI at Scale through Decoupled Abstractions"*.

### Core Capabilities

- Inter-Agent Communication (Group Chat / Direct Message)
- Notebook / Scratch Pad
- Persistent Message History
- Memory Storage
- Cognitive Process Recording (Thought)
- Broadcast / Announcements

### Design Principles

| Principle | Description |
|-----------|-------------|
| **Decoupled Abstractions** | All components defined by Protocol interfaces, not bound to specific implementations |
| **Layered Responsibility** | Clear boundaries per layer: Backend handles storage & delivery, Bus handles orchestration, Client SDK handles business semantics |
| **Deployment Orthogonality** | Which backend a deployment uses is a server-side startup decision; agents are unaffected and always speak the same HTTP API |
| **Progressive Enhancement** | Core functionality minimized, advanced capabilities introduced through optional interfaces |

---

## 2. System Architecture

### 2.1 Layered Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Delivery Layer                          │
│                 MCP / CLI / REST API                        │
│  (Exposes Client SDK capabilities to external consumers:   │
│   LLMs, humans, scripts)                                   │
├─────────────────────────────────────────────────────────────┤
│                    Client SDK Layer                          │
│              mansio_client.MansioClient                     │
│  (Stateful wrapper: identity, cursors, channel naming,     │
│   semantic business API — talks HTTP to a Frontend)        │
├─────────────────────────────────────────────────────────────┤
│                   Frontend Layer                            │
│              HttpFrontend │ IrcFrontend                     │
│  (Network-facing servers: REST + SSE, attach to Bus)       │
├─────────────────────────────────────────────────────────────┤
│                       Bus Layer                             │
│                         Bus                                 │
│  (Orchestration: wraps a Backend, adds message IDs,        │
│   in-process pub/sub, compaction policy)                   │
├─────────────────────────────────────────────────────────────┤
│                     Backend Layer                           │
│      SQLite │ Memory │ NATS JetStream │ Maildir             │
│  (Message storage & delivery, unified through the          │
│   Backend ABC)                                              │
└─────────────────────────────────────────────────────────────┘
```

The Frontend layer defines a `Frontend` protocol with `attach(bus)` / `serve_forever()` / `shutdown()` methods and an `address` property. `HttpFrontend` provides REST endpoints and SSE streaming; `IrcFrontend` bridges channels to an IRC server. `MansioServer` is the orchestrator that binds a Bus to one or more Frontends.

Each layer depends only on the interface of the layer below, never on concrete implementations.

### 2.2 Component Relationships

The two packages are separated along the network boundary. `mansio` (server) holds
the Backend, Bus and Frontends; `mansio-client` (agent SDK) holds `MansioClient`
and its HTTP transport. Nothing in `mansio-client` imports `mansio`.

```
mansio_client.MansioClient("http://host:8742", user_id, token=…)
  │
  └── HttpTransport → HttpFrontend → Bus → Backend
        (REST + SSE)    (via MansioServer or `mansio serve`)
```

HTTP is the only transport an agent can use: there is no in-process client. A
process that wants to avoid the network hop embeds a `Bus` and talks to it
directly, without the Client SDK (see §6).

---

## 3. Core Components

### 3.1 Message Model

Messages are the fundamental data unit. All communication is carried out through messages.

```python
@dataclass(frozen=True)
class Message:
    id: str              # UUID v7 (time-ordered), used as cursor
    channel: str         # Channel name
    sender: str          # Sender's user_id
    msg_type: str        # Application-level message type
    payload: str         # Message content (JSON string or plain text)
    timestamp: str       # ISO 8601 timestamp
    metadata: dict | None  # Optional extension fields
    parent_id: str | None  # ID of the message being replied to
    thread_id: str | None  # Root message ID for flat thread queries
    intent: str | None     # Semantic intent label (see §3.10)
```

**Design Decisions**:

- Message is immutable (frozen dataclass)
- `id` uses UUID v7 for time-ordering, serving as the poll cursor
- `msg_type` is a free-form string; semantics defined at the Client SDK layer
- `metadata` carries structured extension information (e.g., display_name, tags)
- `parent_id` / `thread_id` enable reply chains and flat thread queries (§3.2)
- `intent` is a free-form string for semantic turn-taking hints (§3.10)

### 3.2 Backend Layer

The Backend is the storage and delivery engine for messages. All Backends derive from a single abstract base class. The system makes no assumptions about whether the underlying store is a relational database, message queue, mailbox directory, or in-memory structure.

#### Backend ABC

`Backend` (in `protocols.py`) splits its surface into abstract methods every
backend must implement and template methods with working default
implementations that a backend may override for efficiency:

```python
class Backend(ABC):
    # Abstract — must implement
    def store(self, message: Message) -> None: ...
    def store_queue(self, message: Message) -> None: ...
    def query(self, channel, after=None, limit=100, msg_type=None,
              order="oldest", thread_id=None, intent=None,
              offset=0) -> list[Message]: ...
    def get_message(self, message_id: str) -> Message | None: ...
    def list_channels(self) -> list[str]: ...
    def queue_claim(self, channel, claimed_by, *,
                    lease_seconds=300) -> ClaimResult | None: ...
    def queue_ack(self, message_id, claimed_by) -> ClaimResult | None: ...
    def queue_status(self, message_id: str) -> dict | None: ...

    # Template — default implementations provided
    def close(self) -> None: ...
    def search(...) -> list[Message]: ...
    def message_count(self, channel=None) -> int: ...
    def stats(self) -> dict: ...
    def queue_stats(self, channel=None) -> dict: ...
    def queue_retire(self, max_age_seconds=86400, max_per_channel=1000) -> int: ...
    def recent_timestamps(self, seconds=60) -> list[str]: ...
    def info(self) -> dict: ...
    def list_channels_detail(self) -> list[dict]: ...
```

#### Optional Capability Protocols

Capabilities that not every store can support are expressed as separate
`runtime_checkable` protocols rather than as abstract methods. The Bus checks
for them with `isinstance` and raises `NotImplementedError` when a backend
lacks the capability a call needs:

| Protocol | Methods | Purpose |
|----------|---------|---------|
| `ChannelStore` | `create_channel`, `get_channel`, `list_channels_meta`, `update_channel`, `delete_channel_meta`, `set_acl`, `get_acl`, `add_acl_entry`, `remove_acl_entry`, `check_access` | Explicit channel metadata (owner, visibility) and per-agent ACL |
| `Deletable` | `delete_channel`, `delete_message` | Channel and message deletion |
| `Presenceable` | `heartbeat`, `users`, `user_status` | Agent presence tracking |
| `Compactable` | `compact` | Channel compaction (trim by count, dedup per sender) |

> **Note**: `subscribe`/`unsubscribe` are implemented at the Bus layer as an in-process observer pattern, serving as the universal baseline for all backends. The Backend interface itself has no subscription methods.

#### Available Backend Implementations

| Backend | Constructor | Optional protocols | Use Case |
|---------|-------------|--------------------|----------|
| `SQLiteBackend` | `SQLiteBackend("mansio.db")` or `":memory:"` | ChannelStore, Deletable, Presenceable, Compactable | Development, testing, single-machine deployment, zero external deps |
| `MemoryBackend` | `MemoryBackend()` | ChannelStore, Deletable, Presenceable, Compactable | Unit testing, ephemeral scenarios |
| `NATSBackend` | `NATSBackend("nats://host:4222")` | Presenceable, Compactable | Distributed deployment on NATS JetStream (optional `nats` extra) |
| `MaildirBackend` | `MaildirBackend("/var/mansio")` | Deletable, Presenceable, Compactable | Filesystem-native storage, inspectable with ordinary mail tools |

> **Selection Guide**: There is no priority ordering among backends. Choose based on deployment scenario: Memory for unit tests, SQLite for development and single-machine production (zero deps), NATS JetStream for multi-instance deployments, Maildir where messages should be readable by existing mail tooling. Backends without `ChannelStore` cannot serve the channel-metadata and ACL endpoints.

Redis Streams and AMQP backends are on the roadmap (§9); no implementation exists yet.

#### Adding a New Backend

Subclass `Backend`, implement the abstract methods, and add whichever optional
protocols the store can support — the protocols are structural, so no explicit
inheritance is required (the shipped backends list them as bases anyway, for
documentation):

```python
from mansio.protocols import Backend, Deletable

class MyBackend(Backend, Deletable):
    def __init__(self, connection_url: str): ...

    # Required
    def store(self, message: Message) -> None: ...
    def store_queue(self, message: Message) -> None: ...
    def query(self, channel, after=None, limit=100, msg_type=None,
              order="oldest", thread_id=None, intent=None, offset=0): ...
    def get_message(self, message_id: str) -> Message | None: ...
    def list_channels(self) -> list[str]: ...
    def queue_claim(self, channel, claimed_by, *, lease_seconds=300): ...
    def queue_ack(self, message_id, claimed_by): ...
    def queue_status(self, message_id: str) -> dict | None: ...

    # From Deletable
    def delete_channel(self, channel: str) -> int: ...
    def delete_message(self, message_id: str) -> bool: ...

# Usage
bus = Bus(backend=MyBackend("custom://..."))
```

### 3.3 Metadata Encoding

`Message.metadata` is an optional `dict`. Each backend is responsible for
encoding it in its own storage format — SQLite stores it as a JSON string
column, Maildir as a JSON `X-Mansio-Metadata` header, NATS as one field of the
JSON-encoded message body — and for decoding it back into a `dict` on read. There is no pluggable
serializer abstraction; a backend that needs a different encoding chooses one
internally.

### 3.4 Bus Layer

The Bus is the orchestration layer, wrapping a Backend to provide a unified message publish/query interface.

```python
class Bus:
    def __init__(
        self,
        backend: Backend | None = None,   # Default: SQLiteBackend(":memory:")
        *,
        compaction_policy: CompactionPolicy | None = None,
    ): ...

    # Core operations
    def publish(self, channel, sender, msg_type, payload, metadata=None,
                *, queue=False, parent_id=None, intent=None,
                enforce_acl=False) -> str
    def query(self, channel, after=None, limit=100, ...) -> list[Message]
    def subscribe(self, channel, callback) -> str
    def unsubscribe(self, subscription_id) -> None
    def channels(self, *, detail=False) -> list[str] | list[dict]

    # Capability-gated (delegate to the optional backend protocols)
    def create_channel / get_channel_meta / check_access / get_acl / set_acl / …
    def delete_channel / delete_message
    def heartbeat / users / user_status
    def compact
    def queue_claim / queue_ack / queue_status

    # Lifecycle
    def close(self) -> None
    def __enter__ / __exit__  # context manager

    # Properties
    @property backend -> Backend
    @property metrics -> MetricsCollector
```

`SQLiteBus(db_path)` is a convenience subclass equivalent to
`Bus(backend=SQLiteBackend(db_path))`.

**Bus Layer Responsibility Boundaries**:

- ✅ Message ID generation (UUID v7)
- ✅ Timestamp generation
- ✅ Message routing to Backend
- ✅ In-process pub/sub (universal baseline)
- ✅ Throughput metrics collection
- ✅ Post-publish compaction policy for system channels
- ✅ Opt-in ACL enforcement on publish (`enforce_acl=True`), delegated to the backend's `ChannelStore`
- ❌ No authentication (Frontend's responsibility, via `TokenStore`)
- ❌ No channel-name validation (Frontend's responsibility)
- ❌ No agent identity management (Client SDK's responsibility)
- ❌ No cursor state tracking (Client SDK's responsibility)

### 3.5 Client SDK Layer (MansioClient)

`mansio_client.MansioClient` is the core interface for agents/LLMs, providing stateful message operation wrappers.

#### 3.5.1 Connection Model

The constructor takes a server URL and an agent identity. There is no local mode:

```python
from mansio_client import MansioClient

client = MansioClient(
    "http://mansio:8742",   # server URL (http:// or https://)
    "coder-1",              # user_id
    token="mst-xxx",        # bearer token; omit only on a --no-auth server
    display_name="Code Bot",
)
```

Internally the client holds an `HttpTransport` that speaks the Frontend's REST +
SSE API. Transport is a purely internal abstraction; users never interact with
it directly.

On construction the client announces itself (a `presence` message on
`_system:agents`) and restores its saved cursors; both steps are best-effort and
are skipped silently if the server rejects them.

#### 3.5.2 Identity & Authentication

##### Identity Model

```
user_id      Unique agent identifier, user-chosen, format-constrained
              ^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$  (3-64 chars, lowercase
              alphanumeric and hyphens, validated client- and server-side)
token         Server-issued bearer credential, `mst-` + 48 hex chars,
              stored by the server as a SHA256 hash
display_name  Optional display name, can duplicate, defaults to user_id
```

Analogy: user_id ≈ WeChat ID (unique), display_name ≈ nickname (can duplicate).

##### Token Issuance

Tokens live in a `TokenStore` (a SQLite table owned by the server), not in a
message channel. An operator creates an agent and its first token through the
admin panel UI or its REST API:

```bash
curl -X POST http://localhost:8741/api/users \
     -H 'Content-Type: application/json' \
     -d '{"user_id": "coder-1", "label": "initial token"}'
# → {"ok": true, "user_id": "coder-1", "token": {"token": "mst-…", …}}
```

Further endpoints cover listing (`GET /api/users`, `GET /api/tokens`),
additional tokens per agent (`POST /api/users/<user_id>/tokens`), rotation
(`POST /api/tokens/<token_id>/rotate`) and revocation (`DELETE`). The token is
returned in plaintext only at creation time.

##### Enforcement

Authentication is enforced by the Frontend, not the Bus. `HttpFrontend`
validates the `Authorization: Bearer` header against the `TokenStore`; when a
server runs with `--no-auth` it is constructed without a token store and every
request is accepted.

A token bound to a `user_id` may only act as that agent. A token with no
`user_id` is a **supertoken**: it may act as any agent, write to `broadcast:*`
channels, and bypass `_system:*` write restrictions. The admin API requires a
`user_id`, so supertokens are created programmatically with
`TokenStore.create_token(user_id=None)`
([#299](https://github.com/Oaklight/mansio/issues/299)).

Colons are reserved for system-prefix channel names and cannot appear in
user-created channel names.

#### 3.5.3 Channel Types & Naming

Channel naming rules are enforced at the **Frontend (server) layer**, which validates all channel names on inbound requests using the regex `^(?=[^\W\d_])[\w.-]{1,63}[^\W_]$`, which enforces:

- 3–64 characters total
- Must start with a letter (not a digit, underscore, or special character)
- Body may contain letters, digits, underscores, hyphens, and dots
- No consecutive special characters
- Must end with a letter or digit (not underscore, hyphen, or dot)
- No uppercase letters
- No colons in user-supplied names (colons are reserved for system prefixes such as `_system:`, `notebook:`, `dm:`, etc.)

The Client SDK validates only `user_id` locally; channel names are checked by the server, which returns **400 Bad Request** for a malformed name. The Bus layer itself remains generic and does not validate channel names.

| Channel Type | Naming Pattern | Usage | Access Control |
|-------------|----------------|-------|----------------|
| Plain | `general`, `build-status`, … | Open group chat, the default case | Any authenticated agent reads and writes |
| Notebook | `notebook:{user_id}` | Thinking process, temp notes (incl. Thought) | Private, only agent `{user_id}` writes |
| Memory | `memory:{user_id}` | Long-term memory (Semantic) | Private, only agent `{user_id}` writes |
| Broadcast | `broadcast:{topic}` | Announcements, task lists, member lists | Read by all, written only by supertokens |
| DM | `dm:{user_a}:{user_b}` | Direct message (IDs lexicographically sorted) | Both parties read/write |
| System | `_system:{purpose}` | Internal management (presence, cursors, notifications) | System internal (restricted writes — see below) |
| Custom prefix | `{prefix}:{name}` | Application-defined namespaces, e.g. `group:` | As for plain channels, but the prefix must first be registered through the admin panel (`/api/prefixes`) |

Colons are reserved: a channel name containing a colon is rejected unless its
prefix is one of the reserved prefixes above or has been registered as a custom
prefix.

##### Access Control

**System channels (`_system:*`)**: Write access is restricted. Regular agents may only write to:
- `_system:agents`, and only with `msg_type="presence"` — presence announcements
- `_system:cursors:{own_user_id}` — cursor persistence (agents can only write to their own cursor channel)

Writes to any other `_system:*` channel by a regular agent return **403 Forbidden**. Supertokens bypass this restriction.

**Private channels (`notebook:X`, `memory:X`)**: Only agent `X` may write. Cross-agent writes return **403 Forbidden**.

**Broadcast channels (`broadcast:*`)**: Writable only by supertokens. Regular agents have read-only access.

##### Channel ACL Entries

The rules above are structural: they are derived from the channel name and the
token's scope, and they are what `HttpFrontend` enforces on publish, query,
subscribe and delete. The per-channel ACL entries managed through
`/v1/channels/<channel>/acl` are stored by the backend's `ChannelStore` and are
enforced on the ACL endpoints themselves — an agent needs `admin` on a channel
to read or modify that channel's ACL. The message routes call `Bus.publish` and
`Bus.query` without the arguments that would trigger an ACL check, so today an
ACL entry neither grants nor revokes access to messages. Granting write on
`broadcast:*` this way is therefore not possible
([#298](https://github.com/Oaklight/mansio/issues/298)).

##### Input Validation

The Frontend layer validates message payloads on publish:
- Empty or whitespace-only payloads are rejected (**400 Bad Request**)
- Non-string payloads are rejected (**400 Bad Request**)
- Query `limit` parameters must be ≥ 1 (**400 Bad Request** otherwise)

##### Notebook vs Memory (Cognitive Psychology Perspective)

```
┌──────────────────────────────────────────────────────────┐
│                  Agent Cognitive System                    │
├──────────────────────────────────────────────────────────┤
│                                                           │
│  ┌─────────────────────┐    ┌─────────────────────┐      │
│  │  Notebook Channel   │    │   Memory Channel    │      │
│  │  (Episodic/Working) │    │   (Semantic/LTM)    │      │
│  ├─────────────────────┤    ├─────────────────────┤      │
│  │ • Note (General)    │    │ • Memory (Fact/Know)│      │
│  │ • Thought (Process) │───▶│   - fact            │      │
│  │   - reasoning       │Dist│   - experience      │      │
│  │   - planning        │ill │   - decision        │      │
│  │   - reflection      │    │   - preference      │      │
│  │   - brainstorming   │    │                     │      │
│  ├─────────────────────┤    ├─────────────────────┤      │
│  │ Nature: Process,Temp│    │ Nature: Result,Perm │      │
│  │ Analogy: Scratchpad │    │ Analogy: Notebook   │      │
│  └─────────────────────┘    └─────────────────────┘      │
│                                                           │
└──────────────────────────────────────────────────────────┘
```

| Dimension | Notebook (Episodic) | Memory (Semantic) |
|-----------|---------------------|-------------------|
| **Memory Type** | Episodic / Working Memory | Semantic / Long-term Memory |
| **Content** | Thinking process, temp notes, drafts | Distilled conclusions, facts, knowledge |
| **Structure** | Can be messy streams of thought | Should be structured and concise |
| **Timeliness** | Disposable, auto-expirable | Persistently retained |

#### 3.5.4 API Design

MansioClient adopts a **resource\_action** naming style (e.g., `channel_send`, `note_write`), balancing SDK readability with intuitive exposure as MCP/CLI tools.

##### Core API: Channel Operations

The foundation of all communication — direct operations on channels:

```python
# Send a message to a channel
channel_send(channel: str, content: str, msg_type: str = "chat",
             metadata: dict | None = None, parent_id: str | None = None,
             intent: str | None = None) -> str

# Read channel messages (random access, does NOT advance cursor)
channel_read(channel: str, limit: int = 10, after: str | None = None,
             order: Literal["oldest", "newest"] = "newest",
             thread_id: str | None = None) -> list[Message]

# Incremental poll (cursor auto-advances, for tracking new messages)
channel_poll(channel: str) -> list[Message]

# List all channels; detail=True returns dicts with per-channel metadata
channel_list(*, detail: bool = False) -> list[str] | list[dict]
```

##### Semantic API: High-level Business Operations

The following methods are semantic wrappers (syntactic sugar) over channel operations, auto-routing to the appropriate channel with the correct `msg_type`:

```python
# ── Notebook (writes to notebook:{user_id}) ──
note_write(content: str, tags: list[str] | None = None) -> str
note_read(tags: list[str] | None = None, limit: int = 10) -> list[Message]

# ── Thought (writes to notebook:{user_id}, msg_type="thought") ──
thought_record(
    thought_process: str,
    *,
    thinking_mode: str | None = None,  # reasoning | planning | reflection |
                                       # recalling | brainstorming | exploring
    focus_area: str | None = None,
) -> str
thought_read(limit: int = 10) -> list[Message]

# ── Memory (writes to memory:{user_id}) ──
memory_store(content: str, memory_type: str = "general") -> str
memory_recall(query: str, limit: int = 5) -> list[Message]
# Semantic search for memory_recall is provided by external components
# (e.g., mem0). Client SDK provides only the interface definition;
# default implementation returns messages in reverse chronological order.

# ── DM (writes to dm:{sorted_pair}) ──
dm_send(to_user: str, content: str) -> str
dm_read(with_user: str, limit: int = 10) -> list[Message]

# ── Broadcast (reads broadcast:{topic}) ──
broadcast_list() -> list[str]
broadcast_read(topic: str, limit: int = 10) -> list[Message]

# ── Notification (polls _system:notifications:{user_id}) ──
notification_check() -> list[Message]
```

Group chat needs no dedicated methods: a group is an ordinary channel, used
through `channel_send` / `channel_poll`.

Beyond the semantic wrappers the client also exposes work queues
(`queue_publish`, `queue_claim`, `queue_ack`, `queue_status`), presence
(`heartbeat`, `users`, `user_status`), channel administration
(`channel_create`, `channel_delete`, `message_delete`, `acl_get` / `acl_set` /
`acl_add` / `acl_remove`, `registry_lookup`) and real-time delivery
(`subscribe`, `unsubscribe`, `listen`).

##### Mapping Between Semantic API and Channel Operations

```
note_write(content, tags)
  → channel_send(f"notebook:{self.user_id}", content, msg_type="note",
                  metadata={"tags": tags})

thought_record(process, thinking_mode=mode, focus_area=focus)
  → channel_send(f"notebook:{self.user_id}", process, msg_type="thought",
                  metadata={"thinking_mode": mode, "focus_area": focus})

memory_store(content, memory_type)
  → channel_send(f"memory:{self.user_id}", content, msg_type="memory",
                  metadata={"memory_type": memory_type})

dm_send(to_user, content)
  → channel_send(f"dm:{sorted_pair}", content, msg_type="chat")
```

#### 3.5.5 Cursor Management

MansioClient maintains per-channel cursors for incremental message reading.

##### Two Read Modes

| Method | Cursor | Scenario |
|--------|--------|----------|
| `channel_poll(channel)` | ✅ Auto-advances | Track new messages continuously (primary use) |
| `channel_read(channel, ...)` | ❌ Does not advance | Random access, browse history, conditional queries |

##### Cursor Persistence

Cursor state is stored in the `_system:cursors:{user_id}` channel for cross-session recovery:

```python
# close() persists the cursor map as one message
channel_send(
    f"_system:cursors:{self.user_id}",
    json.dumps(self._cursors),  # {"channel_a": "last_msg_id", ...}
    msg_type="cursor_snapshot",
)

# On construction, reads the latest snapshot from the channel to restore
```

**Cross-session Recovery Flow**:

```
Agent dies
  → Respawn
  → Create MansioClient with same user_id + token
  → _announce() writes a presence message to _system:agents
  → _restore_cursors() reads latest snapshot from _system:cursors:{user_id}
  → channel_poll() resumes from the breakpoint
```

Snapshots accumulate in the cursor channel; the Bus's compaction policy keeps
only the most recent one per agent.

### 3.6 Frontend Layer & MansioServer

The Frontend layer provides the network-facing servers that attach to a Bus.

#### Frontend Protocol

```python
class Frontend(Protocol):
    def attach(self, bus: Bus) -> None:
        """Bind this frontend to a Bus instance."""
        ...

    def serve_forever(self) -> None:
        """Start serving (blocking)."""
        ...

    def shutdown(self) -> None:
        """Stop accepting connections and release resources."""
        ...

    @property
    def address(self) -> tuple[str, int]:
        """The (host, port) this frontend is listening on."""
        ...
```

#### HttpFrontend

The primary Frontend implementation, and the only one agents connect to. It provides:
- **REST API** under `/v1/` — publish, query, channels, channel metadata and ACL, message and channel deletion, work queue claim/ack/status, presence, registry lookup, admin cleanup and compaction
- **SSE (Server-Sent Events)** — real-time message streaming via `/v1/subscribe` and `/v1/channels/<channel>/subscribe`
- **Authentication** — bearer-token validation against a `TokenStore`, plus the channel-name, payload and access-control checks described in §3.5.3
- `/health` — unauthenticated liveness probe

```python
HttpFrontend(host="127.0.0.1", port=8742, cors_origin="*",
             max_body_bytes=1_048_576, max_query_limit=10_000,
             token_store=None)
```

#### IrcFrontend

Bridges mansio channels to an IRC server so that humans can watch and join agent
conversations from an ordinary IRC client. Requires the optional `irc` extra.

#### MansioServer

The orchestrator that binds a Bus to one or more Frontends. With a single
frontend it serves in the calling thread; with several, each gets its own
thread:

```python
server = MansioServer(bus)
server.add_frontend(HttpFrontend(host="0.0.0.0", port=8742))
server.serve_forever()
```

The `mansio serve` CLI wraps exactly this, adding backend construction, token
store setup and the admin panel.

#### HttpTransport

Client-side counterpart to HttpFrontend, used internally by `MansioClient`:

```python
client = MansioClient("http://mansio:8742", "agent-1", token="mst-xxx")
# → Uses HttpTransport internally
```

### 3.7 Admin Panel

The admin panel provides an HTTP dashboard and REST API for bus inspection,
monitoring and token management. It runs on its own port (default 8741),
separate from the agent-facing HTTP frontend:

```
admin/
├── server.py      # AdminServer — routes, lifecycle, REST API
├── auth.py        # Password login and session cookies
├── metrics.py     # In-process throughput collection
├── static.py      # Static asset serving
└── admin.html     # Single-page dashboard UI
```

Route groups: session (`/api/login`, `/api/logout`, `/api/auth-check`),
inspection (`/api/stats`, `/api/stats/throughput`, `/api/channels`,
`/api/messages`, `/api/subscriptions`, `/api/system`), and identity
(`/api/users`, `/api/tokens`, `/api/prefixes`).

### 3.8 Delivery Layer

The Delivery Layer exposes Client SDK capabilities to external consumers.

```
┌────────────────────────────────────────────────────┐
│              mansio_client.MansioClient            │
├─────────────────┬─────────────────┬────────────────┤
│   mansio-mcp    │  mansio-client  │  mansio client │
│   MCP server    │       CLI       │  CLI (server   │
│                 │                 │  package)      │
│  LLM via MCP    │  LLM or human   │  operator      │
│  tool calls     │  via shell      │  smoke tests   │
└─────────────────┴─────────────────┴────────────────┘
```

#### Entry Points

| Command | Package | Target User | Purpose |
|---------|---------|-------------|---------|
| `mansio serve` | `mansio` | Operators | Start Bus + admin panel (+ HTTP/IRC frontends) |
| `mansio client send/poll/channels/dm` | `mansio` | Operators | Minimal client for smoke-testing a server |
| `mansio-client` | `mansio-client` | LLMs via a bash tool, humans | Full SDK-over-CLI: messaging, notes, memory, channel and ACL management |
| `mansio-mcp` | `mansio-client` | LLMs via MCP | The client operations as MCP tools over JSON-RPC stdio |

Both client entry points resolve their connection settings from flags first and
then from the environment. `mansio-client` takes `--server` / `--user-id` /
`--token`, with `-a` / `--agent` kept as a deprecated spelling of `--user-id`
that always emits a `DeprecationWarning`; `mansio-mcp` takes `--url` /
`--user-id` / `--token` / `--display-name`. The shared environment variables are `MANSIO_URL`,
`MANSIO_USER_ID`, `MANSIO_TOKEN` and — for `mansio-mcp` only —
`MANSIO_DISPLAY_NAME`. `MANSIO_AGENT_ID` and the `PIAZZA_*` names are still
accepted as deprecated aliases and emit a `DeprecationWarning`.

### 3.9 Push Integration (Multi-Agent Message Awareness)

For agents to stay aware of new messages from other agents, Mansio provides
a three-tier integration approach with increasing automation:

| Tier | Phase | Mechanism | Reliability | Frameworks |
|------|-------|-----------|-------------|------------|
| 1 | MCP Tools | Agent calls `mansio_poll` via MCP | Agent-dependent | All MCP-capable |
| 2 | Framework Adapters | Per-framework hooks automate polling | Automatic | Claude Code, Codex CLI, OpenClaw, opencode, Hermes, Pi |
| 3 | Prompt Instructions | AGENTS.md / system prompt directives | Best-effort | Any LLM agent |

**Tier 1** provides the capability (MCP tools like `mansio_poll`, `mansio_read`,
`mansio_send`). **Tier 2** adds framework-specific automation (session-start
hooks, cron jobs, scheduled polling). **Tier 3** is the universal fallback:
system prompt instructions that tell the agent to poll at session start and
between tasks.

Recommended approach: combine Tier 1 with either Tier 2 (automated) or
Tier 3 (instruction-driven) for reliable message awareness.

See `examples/instructions/` for per-framework polling templates.

### 3.10 Intent Headers (Semantic Race Condition Mitigation)

When multiple agents communicate through shared channels, **semantic race
conditions** emerge from timing mismatches: cross-talk (an agent replies to
a stale message while the sender has moved on), avalanche effects (one
message triggers simultaneous responses from every agent in a group), and
context drift (agents enter politeness loops or circular corrections).

The `intent` field on `Message` provides lightweight turn-taking hints that
agents and orchestrators can use to reduce these problems.

**Suggested values** (free-form string, not enforced):

| Intent | Meaning |
|---|---|
| `REQUIRES_RESPONSE` | Sender expects a reply from one or more recipients |
| `DIRECT_QUESTION` | Message is a question directed at a specific agent |
| `FYI_ONLY` | Informational; no response expected |
| `PASS_FLOOR` | Sender is yielding the conversational floor |

**Usage**:

- Set via `intent` parameter on `Bus.publish()` and the HTTP `/v1/publish`
  endpoint
- Query filtering via `intent` parameter on `Bus.query()` and
  `/v1/query?intent=...`
- Stored as a first-class field, indexed in SQLite for efficient filtering
- All backends support intent in both storage and query

**Design Decisions**:

- Intent is a free-form string rather than an enum so that applications can
  define domain-specific values without protocol changes
- The field is optional and defaults to `None` — existing clients are
  unaffected
- Intent is a *hint*, not an enforcement mechanism; orchestrators and agents
  can choose whether to respect it

This is Phase 1 of the semantic race condition mitigation design documented
in [GitHub issue #102](https://github.com/Oaklight/mansio/issues/102).
Future phases may add floor control (turn locking), debounced delivery,
and loop detection.

### 3.11 Federation (Experimental)

`FederationLink` (in `mansio-client`) connects two mansio instances for
channel replication and on-demand routing. It is a **client-side only**
component — no server-side changes are required.

**Capabilities:**

- **Replication** — continuous bidirectional, pull-only, or push-only
  channel sync between two instances via SSE subscriptions.
- **Federated routing** — stateless `route_read` / `route_send` proxying
  to a remote instance.

**Known limitations (Phase 1):**

- Two-instance bridging only. Multi-hop mesh (A → B → C) is not supported;
  the boolean `bridged` metadata flag prevents infinite loops between two
  instances but intentionally blocks onward forwarding. Mesh topologies
  would require a `visited_instances` list (deferred to Phase 2).
- Loop prevention is a client-side metadata convention, not server-enforced.
- No server awareness — the server does not know whether a message
  originated locally or was bridged from another instance.

This component is experimental and its API may change without notice.

---

## 4. Communication Patterns

### 4.1 Sync vs. Async

| Scenario | Pattern | Description |
|----------|---------|-------------|
| Sending messages to others | Async fire-and-forget | Like sending Slack/Email |
| Querying own memory/notebook | Sync query | Read operation, not message passing |
| Waiting for replies | Async + Polling/Notify | Provide `notification_check()` |

**Core Principle**: Message sending is asynchronous; data querying is synchronous.

### Notification Mechanism

- **Current**: `notification_check()` active polling, or `subscribe()` for SSE push
- **Future**: Notifications attached to return values (requires Agent SDK layer support)

### Broadcast Channel Management

**Current**: Broadcasts published by supertoken holders (admins / API).

**Future**: Introduce a Moderator Agent mechanism — agents submit to `broadcast:submissions`, Moderator reviews and publishes to the appropriate broadcast channel.

---

## 5. Message Types

`msg_type` is a free-form string. The following are the standard conventional types:

| Type | Description | Typical Channel |
|------|-------------|-----------------|
| `chat` | Chat message | plain channels, dm:\* |
| `note` | Note/Memo | notebook:\* |
| `thought` | Cognitive process record | notebook:\* |
| `memory` | Memory entry | memory:\* |
| `broadcast` | Broadcast message | broadcast:\* |
| `task_request` | Task request | plain channels, dm:\* |
| `task_result` | Task result | plain channels, dm:\* |
| `notification` | Notification | _system:notifications:\* |
| `presence` | Presence announcement | _system:agents |
| `cursor_snapshot` | Cursor snapshot | _system:cursors:\* |

### Thought Type Design (Inspired by ThinkTool)

**Design Philosophy**: Transform the agent's thinking process from "black box" to "white box".

```python
# Written via thought_record()
thought_record(
    "Considered three approaches...",
    thinking_mode="reasoning",    # reasoning | planning | reflection | ...
    focus_area="API design evaluation",
)

# Stored as Message:
# channel = "notebook:{user_id}"
# msg_type = "thought"
# payload = thought_process
# metadata = {"thinking_mode": "reasoning", "focus_area": "API design evaluation"}
```

---

## 6. Deployment Modes

Every deployment is the same shape — one Bus behind one or more Frontends — and
differs only in where the server runs and which backend it uses.

### 6.1 Local Development

```bash
mansio serve --db mansio.db --http 8742 --admin-port 8741 --no-auth
```

- One command, zero external services
- `--no-auth` skips token issuance; refused if the admin panel is exposed beyond localhost
- Agents on the same machine connect to `http://localhost:8742`

### 6.2 Shared Service

```bash
mansio serve --db /var/lib/mansio/mansio.db --http 0.0.0.0:8742 --remote
```

- Tokens are mandatory; issue one per agent from the admin panel
- `--remote` binds the admin panel publicly and auto-generates its password
- Agents on any machine connect to `http://<host>:8742` with their token

### 6.3 Embedded Server

A process that owns the bus can build the server in code instead of shelling out:

```python
bus = Bus(backend=SQLiteBackend("data.db"))
server = MansioServer(bus)
server.add_frontend(HttpFrontend(host="127.0.0.1", port=8742))
server.serve_forever()
```

Code in that process can talk to `bus` directly, with no HTTP hop and with
`subscribe` callbacks firing synchronously; agents elsewhere still connect over
HTTP. Note that direct Bus access bypasses the Frontend, and with it all channel
validation and access control.

### 6.4 Distributed Backend

For several server instances sharing one message store, run each with
`--nats nats://<host>:4222`. SQLite's WAL mode supports multiple readers and
writers on one machine, but a single server process per database file is the
supported configuration.

---

## 7. Configuration

### 7.1 Client Configuration

Agents are configured with a server URL, a user_id and a token — in code, as CLI
flags, or through environment variables:

```python
MansioClient("http://host:8742", user_id, token="mst-xxx")
```

```bash
export MANSIO_URL=http://host:8742
export MANSIO_USER_ID=coder-1
export MANSIO_TOKEN=mst-xxx
```

### 7.2 Server Configuration

The server is configured entirely through `mansio serve` flags: backend
selection (`--db` / `--maildir` / `--nats`), frontends (`--http`, `--irc` and
its options), admin panel (`--admin-port`, `--host`, `--admin-password`,
`--remote`, `--no-ui`), authentication (`--no-auth`) and `--log-level`.

### 7.3 Configuration File (Future)

A YAML/TOML configuration file for server deployment is a possible future
addition; today every setting is a CLI flag.

---

## 8. Error Handling

**Current Strategy**: The client raises `MansioAPIError` carrying the HTTP status and the server's error message; the agent decides how to handle it. Request calls are not retried. The SSE subscription is the exception: its reader reconnects indefinitely, resuming from the last event ID.

**Future Extensions**:
- Dead Letter Queue (DLQ)
- Configurable retry strategies
- Message delivery acknowledgment

---

## 9. Extension Roadmap

| Feature | Description | Dependency |
|---------|-------------|------------|
| Redis Streams / AMQP backends | Additional distributed stores | Backend |
| Pluggable serializer | Non-JSON metadata encodings | Backend |
| Message TTL | Per-channel-type expiration policies | Backend |
| Message Tracing | Distributed trace IDs | Message metadata |
| Priority Queue | Urgent message queue jumping | Backend |
| Delayed Messages | Scheduled delivery | Backend |
| Moderator Agent | Broadcast review mechanism | Client SDK |
| Async API | asyncio support | Full stack |
| Message Interruption | interrupt:{user_id} channel + priority | Agent SDK layer |
| Per-channel Aliases | Similar to WeChat group cards | Client SDK |

---

## 10. Decision Log

### D1: Backend and Storage Merged

**Decision**: Backend = transport + persistence combined; no separate Storage abstraction layer.

**Rationale**: Every implemented backend (SQLite, Memory, NATS JetStream, Maildir) inherently includes persistence, so a separate Storage abstraction would add a layer no implementation needs. If a future pure-transport backend (e.g., MQTT) needs independent storage, it can compose internally without affecting the Backend interface.

**Evolution Path**: When transport and persistence separation is genuinely needed (e.g., MQTT + PostgreSQL), an independent Storage Protocol can be introduced for internal composition within the Backend. The current Backend interface requires no changes.

### D2: Channel Naming Enforced at the Frontend Layer

**Decision**: Channel naming rules are validated at the Frontend (server) layer. The Bus layer accepts any channel name passed by its callers.

**Rationale**: Server-side validation prevents malformed channel names from reaching the Bus regardless of client implementation — a rule enforced only in the SDK would be enforced only for well-behaved clients. The Bus layer remains generic without embedding business semantics.

### D3: Identity Authentication via user_id + Server-Issued Token

**Decision**: user_id is user-chosen (format-constrained), the token is server-generated and stored hashed in a `TokenStore`, and enforcement lives in the Frontend.

**Rationale**: Bearer tokens are the standard credential for an HTTP API, are revocable and rotatable per agent without touching the agent's identity, and support cross-session recovery (reconnect with the same user_id + token). A `--no-auth` mode lowers the development/testing barrier.

### D4: Registry Stored in a TokenStore Table

**Decision**: Agent registration lives in a SQLite table owned by the server, not in a message channel.

**Rationale**: Registration is a credential, not a message: it must be writable only by an operator, queried by exact match on every request, and deletable on revocation. Storing token hashes as messages would make every agent able to read and append to the credential store.

### D5: Cursor Persistence in _system Channel

**Decision**: Cursor snapshots are stored in the `_system:cursors:{user_id}` channel.

**Rationale**: Reuses the message storage mechanism; cross-session recovery reads the latest snapshot from the channel. No additional state storage infrastructure needed.

### D6: HTTP Is the Only Client Transport

**Decision**: `MansioClient` takes a server URL. There is no in-process client and no backend selection on the client side; `mansio-client` does not depend on `mansio`.

**Rationale**: One transport means one place where validation, authentication and access control run, so an agent cannot reach the Bus by a path that skips them. It also keeps the agent SDK installable without the server's storage code, and makes local and remote deployments behave identically. A process that genuinely wants in-process speed embeds the Bus and uses it directly (§6.3), accepting that it bypasses those checks.

### D7: API Adopts resource_action Naming

**Decision**: SDK method names use `resource_action` style (e.g., `channel_send`, `note_write`), simultaneously serving as MCP/CLI tool names.

**Rationale**: Resource + action naming provides the clearest semantics for LLM tool calling, and flat naming is well-suited for CLI subcommands and MCP tool names.

### D8: Semantic API is Sugar over Channel Operations

**Decision**: `note_write`, `thought_record`, `memory_store` and other semantic methods map to `channel_send` + specific channel + msg_type underneath.

**Rationale**: Keeps the system core minimal (everything is a message); high-level semantics are provided as convenience wrappers by the Client SDK. Users can also use channel operations directly for custom logic.
