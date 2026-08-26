# Agent Messaging Hub — Design Document

## 1. Project Overview

Mansio is a messaging backbone system designed for LLMs/Agents, providing unified communication infrastructure for multi-agent collaboration. This project serves as the reference implementation of the **Messaging** component (Chapter 9) in the PhD dissertation *"Enabling Agentic AI at Scale through Decoupled Abstractions"*.

### Core Capabilities

- Inter-Agent Communication (Group Chat / Direct Message)
- Notebook / Scratch Pad
- History Tracking
- Memory Storage
- Cognitive Process Recording (Thought)
- Broadcast / Announcements

### Design Principles

| Principle | Description |
|-----------|-------------|
| **Decoupled Abstractions** | All components defined by Protocol interfaces, not bound to specific implementations |
| **Layered Responsibility** | Clear boundaries per layer: Backend handles storage & delivery, Bus handles orchestration, Client SDK handles business semantics |
| **Connection String Driven** | Deployment decisions (which backend) are orthogonal to architecture, selected at runtime via connection strings |
| **Progressive Enhancement** | Core functionality minimized, advanced capabilities introduced through optional interfaces |

---

## 2. System Architecture

### 2.1 Layered Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Delivery Layer                          │
│            MCP / REST API / CLI / OpenAPI                   │
│  (Exposes Client SDK capabilities to external consumers:   │
│   LLMs, humans, scripts)                                   │
├─────────────────────────────────────────────────────────────┤
│                    Client SDK Layer                          │
│                     MansioClient                            │
│  (Stateful wrapper: identity, cursors, channel naming,     │
│   semantic business API)                                    │
├─────────────────────────────────────────────────────────────┤
│                   Frontend Layer  🔄                        │
│            HttpFrontend / (future: IRC, WS)                │
│  (Network-facing servers: REST + SSE, attach to Bus)       │
├─────────────────────────────────────────────────────────────┤
│                       Bus Layer                             │
│                         Bus                                 │
│  (Orchestration: composes Backend + Serializer,            │
│   provides pub/sub)                                        │
├─────────────────────────────────────────────────────────────┤
│                     Backend Layer                           │
│          SQLite │ Redis │ RabbitMQ │ ...                     │
│  (Message storage & delivery, unified through Protocol     │
│   interface)                                                │
└─────────────────────────────────────────────────────────────┘
```

> **🔄 In Progress (`dev/agent-bus`):** The Frontend layer introduces a `Frontend` protocol with `attach(bus)` / `serve_forever()` / `shutdown()` methods. `HttpFrontend` is the first implementation, providing REST endpoints and SSE streaming. `MansioServer` is the orchestrator that binds a Bus to one or more Frontends.

Each layer depends only on the Protocol interface of the layer below, never on concrete implementations.

### 2.2 Component Relationships

```
MansioClient(target)
  │
  ├── target = Bus object  → Bus → Backend
  ├── target = "mansio.db" → auto-create Bus(SQLiteBackend)
  ├── target = "redis://…" → auto-create Bus(RedisBackend)
  └── target = "http://…"  → HttpTransport → HttpFrontend → Bus → Backend
                                               (via MansioServer)
```

The Client SDK shields local/remote differences through the Transport abstraction, fully transparent to upper layers.

> **Note:** The `Transport` protocol now includes `subscribe(channel, callback)` and `unsubscribe(subscription_id)` to support real-time event delivery across both local and remote transports.

---

## 3. Core Components

### 3.1 Message Model

Messages are the fundamental data unit. All communication is carried out through messages.

```python
@dataclass(frozen=True)
class Message:
    id: str              # UUID v7 (time-ordered), used as cursor
    channel: str         # Channel name
    sender: str          # Sender's agent_id
    msg_type: str        # Application-level message type
    payload: str         # Message content (JSON string or plain text)
    timestamp: str       # ISO 8601 timestamp
    metadata: dict | None  # Optional extension fields
```

**Design Decisions**:

- Message is immutable (frozen dataclass)
- `id` uses UUID v7 for time-ordering, serving as the poll cursor
- `msg_type` is a free-form string; semantics defined at the Client SDK layer
- `metadata` carries structured extension information (e.g., display_name, tags)

### 3.2 Backend Layer

The Backend is the storage and delivery engine for messages. All Backends interface through a unified Protocol. The system makes no assumptions about whether the underlying store is a relational database, message queue, or in-memory structure.

#### Backend Protocol

```python
class Backend(Protocol):
    """Unified interface for message backends."""

    def store(self, message: Message) -> None:
        """Persist a message."""
        ...

    def query(
        self, channel: str,
        after: str | None = None,
        limit: int = 100,
    ) -> list[Message]:
        """Query messages by channel with cursor-based pagination."""
        ...

    def channels(self) -> list[str]:
        """List all channels that contain messages."""
        ...

    def subscribe(
        self, channel: str,
        callback: Callable[[Message], None],
    ) -> str:
        """Register a message delivery callback, return subscription ID.

        Different backends implement this according to their capabilities:
        - Polling-based backends (SQLite): Bus layer provides in-process observer
        - Native pub/sub backends (Redis): leverage native subscription
        - Message queue backends (RabbitMQ): leverage consumer mechanism
        """
        ...

    def unsubscribe(self, subscription_id: str) -> None:
        """Cancel a subscription."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...
```

> **Note**: `subscribe`/`unsubscribe` are currently implemented at the Bus layer as an in-process observer pattern, serving as the universal baseline for all backends. When a backend has native push capabilities, the Bus layer can delegate subscriptions to the backend for more efficient delivery.

#### Available Backend Implementations

| Backend | Connection String | Use Case |
|---------|------------------|----------|
| SQLiteBackend | `mansio.db` or `:memory:` | Development, testing, single-machine deployment, zero external deps |
| MemoryBackend | `:memory:` (via Bus object) | Unit testing, ephemeral scenarios |
| RedisBackend | `redis://host:port` | Multi-instance deployment, native pub/sub needed |
| RabbitMQBackend | `amqp://host:port` | Enterprise-grade, complex routing, durable queues |
| *Custom* | *Custom URL scheme* | Extend as needed |

> **Selection Guide**: There is no priority ordering among backends. Choose based on deployment scenario: SQLite/Memory for development and testing (zero deps), SQLite for single-machine production, Redis for cross-instance communication, RabbitMQ for enterprise messaging guarantees.

#### Adding a New Backend

Implement the `Backend` protocol to integrate:

```python
class MyBackend:
    def __init__(self, connection_url: str): ...
    def store(self, message: Message) -> None: ...
    def query(self, channel, after=None, limit=100) -> list[Message]: ...
    def channels(self) -> list[str]: ...
    def close(self) -> None: ...

# Usage
bus = Bus(backend=MyBackend("custom://..."))
```

### 3.3 Serializer

The Serializer handles encoding/decoding of metadata dictionaries.

```python
class Serializer(Protocol):
    def encode(self, obj: dict) -> str: ...
    def decode(self, data: str) -> dict: ...
```

| Serializer | Characteristics | Use Case |
|-----------|----------------|----------|
| JSONSerializer | Human-readable, debug-friendly | Default, suitable for dev and production |
| MessagePackSerializer | Compact, efficient | High-throughput scenarios |
| *Custom* | As needed | Special protocol requirements |

### 3.4 Bus Layer

The Bus is the orchestration layer, composing Backend and Serializer to provide a unified message publish/query interface.

```python
class Bus:
    def __init__(
        self,
        backend: Backend | None = None,      # Default: SQLiteBackend(:memory:)
        serializer: Serializer | None = None, # Default: JSONSerializer
        require_auth: bool = False,           # Authentication mode toggle
    ): ...

    # Core operations
    def publish(self, channel, sender, msg_type, payload, metadata=None) -> str
    def poll(self, channel, after=None, limit=100) -> list[Message]
    def subscribe(self, channel, callback) -> str
    def unsubscribe(self, subscription_id) -> None
    def channels(self) -> list[str]

    # Lifecycle
    def close(self) -> None
    def __enter__ / __exit__  # context manager

    # Properties
    @property backend -> Backend
    @property serializer -> Serializer
```

**Bus Layer Responsibility Boundaries**:

- ✅ Message ID generation (UUID v7)
- ✅ Timestamp generation
- ✅ Message routing to Backend
- ✅ In-process pub/sub (universal baseline)
- ✅ Authentication mode control
- ❌ No agent identity management (Client SDK's responsibility)
- ❌ No cursor state tracking (Client SDK's responsibility)

### 3.5 Client SDK Layer (MansioClient)

MansioClient is the core interface for agents/LLMs, providing stateful message operation wrappers.

#### 3.5.1 Connection Model

MansioClient's constructor accepts either a `Bus` object or a connection string, automatically selecting the appropriate Transport:

```python
# Mode 1: Pass Bus object (orchestrator pattern)
bus = Bus(backend=SQLiteBackend("data.db"))
client = MansioClient(bus, "coder-1")

# Mode 2: Pass connection string (auto-creates Bus)
client = MansioClient("mansio.db", "coder-1")
client = MansioClient(":memory:", "coder-1")
client = MansioClient("redis://localhost:6379", "coder-1")
client = MansioClient("amqp://localhost", "coder-1")

# Mode 3: Connect to remote MansioServer
client = MansioClient("http://mansio:8741", "coder-1", secret="sk-xxx")
```

Internal routing via Transport abstraction:

```
Target Type                → Transport         → Bus Lifecycle
───────────────────────────────────────────────────────────────
Bus object                 → Bus            → Caller manages
File path / :memory:       → Bus            → Client creates & manages
redis:// / amqp://         → Bus            → Client creates & manages
http:// / https://         → RemoteTransport   → Remote Server manages
```

Transport is a purely internal abstraction; users never interact with it directly.

#### 3.5.2 Identity & Authentication

##### Identity Model

```
agent_id      Unique system identifier, user-chosen, format-constrained
              (lowercase alphanumeric, hyphens, underscores, dots; 3-64 chars)
secret        Mansio-generated credential, stored as SHA256 hash
display_name  Optional display name, can duplicate, defaults to agent_id
```

Analogy: agent_id ≈ WeChat ID (unique), display_name ≈ nickname (can duplicate).

##### Registration & Connection

```python
# First-time registration
client, secret = MansioClient.register(target, "coder-1", display_name="Code Bot")
# → Generates secret, writes to _system:registry channel
# → Caller saves secret (env var / config)

# Reconnect with secret (cross-session recovery)
client = MansioClient(target, "coder-1", secret="sk-xxx")
# → Validates secret → restores cursors → resumes

# No-auth mode (when Bus require_auth=False)
client = MansioClient(target, "coder-1")
# → Skips authentication, direct use
```

##### Token-Based Authentication

Controlled via Bus startup configuration:

```python
# Development/Testing: no auth (default)
bus = Bus(require_auth=False)

# Production/Shared service: mandatory auth
bus = Bus(require_auth=True)
```

When `require_auth=True`, each agent authenticates with a per-agent secret token. **Supertokens** grant elevated privileges (write to `broadcast:*` channels, bypass `_system:*` write restrictions). Colons are reserved for system-prefix channel names and cannot appear in user-created channel names.

##### Registry Storage

Agent registration information is stored in the `_system:registry` channel, following the "everything is a message" principle:

```python
# Message written during registration
channel = "_system:registry"
sender = agent_id
msg_type = "register"
metadata = {
    "display_name": "Code Bot",
    "secret_hash": "sha256:...",
    "action": "register",  # register | deregister | update
}
```

The Client SDK reads this channel to build the current agent state map.

##### Secret Management

```python
# Reserved interfaces (not yet implemented in MVP)
client.rotate_secret() -> str      # raises NotImplementedError
client.revoke() -> None            # raises NotImplementedError
```

#### 3.5.3 Channel Types & Naming

Channel naming rules are enforced at **both the Frontend (server) and Client SDK layers**. The Frontend layer validates all channel names on inbound requests using the regex `^(?=[^\W\d_])[\w.-]{1,63}[^\W_]$`, which enforces:

- 3–64 characters total
- Must start with a letter (not a digit, underscore, or special character)
- Body may contain letters, digits, underscores, hyphens, and dots
- No consecutive special characters
- Must end with a letter or digit (not underscore, hyphen, or dot)
- No uppercase letters
- No colons in user-supplied names (colons are reserved for system prefixes such as `_system:`, `notebook:`, `dm:`, etc.)

The Client SDK applies the same rules locally for fast feedback. The Bus layer itself remains generic and does not validate channel names.

| Channel Type | Naming Pattern | Usage | Access Control |
|-------------|----------------|-------|----------------|
| History | `history:{agent_id}` | Dialogue/Work log | Private, auto-written by SDK |
| Notebook | `notebook:{agent_id}` | Thinking process, temp notes (incl. Thought) | Private, agent writes actively |
| Memory | `memory:{agent_id}` | Long-term memory (Semantic) | Private |
| Broadcast | `broadcast:{topic}` | Announcements, task lists, member lists | Public read-only |
| Group | `group:{group_id}` | Group chat | Member read/write |
| DM | `dm:{agent_a}:{agent_b}` | Direct message (IDs lexicographically sorted) | Both parties read/write |
| System | `_system:{purpose}` | Internal management (registry, cursors, etc.) | System internal (restricted writes — see below) |

##### Access Control

**System channels (`_system:*`)**: Write access is restricted. Regular agents may only write to:
- `_system:agents` — presence announcements
- `_system:cursors:{own_agent_id}` — cursor persistence (agents can only write to their own cursor channel)
- `_system:registry` — self-registration

Writes to any other `_system:*` channel by a regular agent return **403 Forbidden**. Supertokens bypass this restriction.

**Private channels (`notebook:X`, `memory:X`)**: Only agent `X` may write. Cross-agent writes return **403 Forbidden**.

**Broadcast channels (`broadcast:*`)**: Writable only by supertokens. Regular agents have read-only access.

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
             metadata: dict | None = None) -> str

# Read channel messages (random access, does NOT advance cursor)
channel_read(channel: str, limit: int = 10,
             after: str | None = None) -> list[Message]

# Incremental poll (cursor auto-advances, for tracking new messages)
channel_poll(channel: str) -> list[Message]

# List all channels
channel_list() -> list[str]
```

##### Semantic API: High-level Business Operations

The following methods are semantic wrappers (syntactic sugar) over channel operations, auto-routing to the appropriate channel with the correct `msg_type`:

```python
# ── Notebook (writes to notebook:{agent_id}) ──
note_write(content: str, tags: list[str] | None = None) -> str
note_read(tags: list[str] | None = None, limit: int = 10) -> list[Message]

# ── Thought (writes to notebook:{agent_id}, msg_type="thought") ──
thought_record(
    thinking_mode: str,    # reasoning | planning | reflection | recalling | brainstorming | exploring
    focus_area: str,
    thought_process: str,
) -> str
thought_read(limit: int = 10) -> list[Message]

# ── Memory (writes to memory:{agent_id}) ──
memory_store(content: str, memory_type: str = "general") -> str
memory_recall(query: str, limit: int = 5) -> list[Message]
# Semantic search for memory_recall is provided by external components
# (e.g., mem0). Client SDK provides only the interface definition;
# default implementation returns messages in reverse chronological order.

# ── DM (writes to dm:{sorted_pair}) ──
dm_send(to_agent: str, content: str) -> str
dm_read(with_agent: str, limit: int = 10) -> list[Message]

# ── Group ──
group_create(name: str, members: list[str]) -> str
group_send(group_id: str, content: str) -> str
group_read(group_id: str, limit: int = 10) -> list[Message]

# ── Broadcast ──
broadcast_list() -> list[str]
broadcast_read(topic: str, limit: int = 10) -> list[Message]

# ── Notification ──
notification_check() -> list[Message]
```

##### Mapping Between Semantic API and Channel Operations

```
note_write(content, tags)
  → channel_send(f"notebook:{self.agent_id}", content, msg_type="note",
                  metadata={"tags": tags})

thought_record(mode, focus, process)
  → channel_send(f"notebook:{self.agent_id}", process, msg_type="thought",
                  metadata={"thinking_mode": mode, "focus_area": focus})

memory_store(content, memory_type)
  → channel_send(f"memory:{self.agent_id}", content, msg_type="memory",
                  metadata={"memory_type": memory_type})

dm_send(to_agent, content)
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

Cursor state is stored in the `_system:cursors:{agent_id}` channel for cross-session recovery:

```python
# Client SDK periodically or at key points persists cursors
channel_send(
    f"_system:cursors:{self.agent_id}",
    json.dumps(self._cursors),  # {"channel_a": "last_msg_id", ...}
    msg_type="cursor_snapshot",
)

# On reconnect, reads the latest snapshot from the channel to restore
```

**Cross-session Recovery Flow**:

```
Agent dies
  → Respawn
  → Create MansioClient with same agent_id + secret
  → _announce() writes new register message
  → _restore_cursors() reads latest snapshot from _system:cursors:{agent_id}
  → channel_poll() resumes from the breakpoint
```

### 3.6 Frontend Layer & MansioServer

> 🔄 **In Progress** — implemented on `dev/agent-bus`, pending merge to master.

The Frontend layer enables the Hub-Server deployment mode by providing network-facing servers that attach to a Bus.

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
        """Graceful shutdown."""
        ...
```

#### HttpFrontend

The first Frontend implementation, providing:
- **REST API** — publish, poll, list channels
- **SSE (Server-Sent Events)** — real-time message streaming via `subscribe`

#### MansioServer

The orchestrator that binds a Bus to one or more Frontends:

```python
server = MansioServer(bus)
server.add_frontend(HttpFrontend(host="0.0.0.0", port=8741))
server.serve_forever()
```

#### HttpTransport

Client-side counterpart to HttpFrontend, implementing the Transport protocol over HTTP:

```python
# Client connects to remote MansioServer
client = MansioClient("http://mansio:8741", "agent-1", secret="sk-xxx")
# → Uses HttpTransport internally
```

### 3.7 Admin Panel

The admin panel provides an HTTP dashboard for bus inspection and monitoring. Admin handlers are organized as a modular `admin/routes/` subpackage using dict-based dispatch:

```
admin/
├── server.py          # AdminServer (HTTP server lifecycle)
├── auth.py            # Authentication helpers
├── handlers.py        # Top-level handler + dict-based dispatch
├── static.py          # Static asset serving
└── routes/
    ├── __init__.py    # ROUTE_TABLE (path → handler mapping)
    ├── _shared.py     # Shared utilities (JSON response, error handling)
    ├── channels.py    # /api/channels, /api/channels/{name}
    ├── dashboard.py   # /api/dashboard (stats + throughput)
    ├── messages.py    # /api/messages/{channel}
    ├── subscriptions.py  # /api/subscriptions
    └── ui.py          # / (HTML dashboard)
```

### 3.8 Delivery Layer

The Delivery Layer exposes Client SDK capabilities to external consumers.

```
┌────────────────────────────────────────────────────┐
│                 MansioClient SDK                   │
├───────────┬───────────┬───────────┬────────────────┤
│    MCP    │ REST API  │   CLI     │   OpenAPI      │
│  Server   │  Server   │ (Tier 2)  │   Schema       │
│           │           │           │                │
│  LLM via  │  HTTP     │  LLM via  │  Swagger /     │
│  MCP tool │  clients  │  bash tool│  code-gen      │
└───────────┴───────────┴───────────┴────────────────┘
```

#### CLI Two-Tier Design

| Tier | Target User | Functions |
|------|------------|-----------|
| **Tier 1: Operations** | DevOps | `mansio serve`, `mansio status`, `mansio admin` |
| **Tier 2: SDK-over-CLI** | LLMs (via bash tool) | SDK methods mapped to CLI commands, e.g., `mansio channel send ...` |

#### Delivery Channels

MansioClient methods can be uniformly exposed as MCP tools, REST APIs, and CLI commands via toolregistry-server, without writing separate adapter code for each protocol.

### 3.9 Push Integration (Multi-Agent Message Awareness)

For agents to stay aware of new messages from other agents, Mansio provides
a three-tier integration approach with increasing automation:

| Tier | Phase | Mechanism | Reliability | Frameworks |
|------|-------|-----------|-------------|------------|
| 1 | MCP Tools | Agent calls `mansio_poll` via MCP | Agent-dependent | All MCP-capable |
| 2 | Framework Adapters | Per-framework hooks automate polling | Automatic | Claude Code, Codex, OpenClaw, Hermes, Pi |
| 3 | Prompt Instructions | AGENTS.md / system prompt directives | Best-effort | Any LLM agent |

**Tier 1** provides the capability (MCP tools like `mansio_poll`, `mansio_read`,
`mansio_send`). **Tier 2** adds framework-specific automation (session-start
hooks, cron jobs, scheduled polling). **Tier 3** is the universal fallback:
system prompt instructions that tell the agent to poll at session start and
between tasks.

Recommended approach: combine Tier 1 with either Tier 2 (automated) or
Tier 3 (instruction-driven) for reliable message awareness.

See `examples/instructions/` for per-framework polling templates.

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

- **MVP**: `notification_check()` active polling
- **Future**: Active polling + notifications attached to return values (requires Agent SDK layer support)

### Broadcast Channel Management

**MVP**: Broadcasts published directly by admins / API.

**Future**: Introduce a Moderator Agent mechanism — agents submit to `broadcast:submissions`, Moderator reviews and publishes to the appropriate broadcast channel.

---

## 5. Message Types

`msg_type` is a free-form string. The following are the standard conventional types:

| Type | Description | Typical Channel |
|------|-------------|-----------------|
| `chat` | Chat message | group:\*, dm:\* |
| `note` | Note/Memo | notebook:\* |
| `thought` | Cognitive process record | notebook:\* |
| `memory` | Memory entry | memory:\* |
| `broadcast` | Broadcast message | broadcast:\* |
| `task_request` | Task request | group:\*, dm:\* |
| `task_result` | Task result | group:\*, dm:\* |
| `notification` | Notification | _system:\* |
| `heartbeat` | Heartbeat | _system:\* |
| `register` | Agent registration | _system:registry |
| `cursor_snapshot` | Cursor snapshot | _system:cursors:\* |

### Thought Type Design (Inspired by ThinkTool)

**Design Philosophy**: Transform the agent's thinking process from "black box" to "white box".

```python
# Written via thought_record()
thought_record(
    thinking_mode="reasoning",    # reasoning | planning | reflection | ...
    focus_area="API design evaluation",
    thought_process="Considered three approaches...",
)

# Stored as Message:
# channel = "notebook:{agent_id}"
# msg_type = "thought"
# payload = thought_process
# metadata = {"thinking_mode": "reasoning", "focus_area": "API design evaluation"}
```

---

## 6. Deployment Modes

### 6.1 Embedded (In-process)

All agents run in the same process, sharing a Bus object.

```python
bus = Bus(backend=SQLiteBackend("data.db"))
client_a = MansioClient(bus, "coder-1")
client_b = MansioClient(bus, "reviewer-1")
```

- Simplest, zero network overhead
- Subscribe callbacks fire synchronously in-process
- Suitable for single-machine multi-agent orchestration

### 6.2 Multi-process Shared Storage

Each process independently creates a Client pointing to the same storage.

```python
# Process A
client_a = MansioClient("shared/mansio.db", "coder-1")

# Process B
client_b = MansioClient("shared/mansio.db", "reviewer-1")
```

- Concurrent read/write supported via SQLite WAL mode
- Subscribe only works in-process; cross-process uses `channel_poll()`
- Suitable for single-machine multi-process scenarios

### 6.3 Persistent Service (MansioServer)

Centralized service with Clients connecting via network API.

```python
# Server side
bus = Bus(backend=SQLiteBackend("data.db"), require_auth=True)
server = MansioServer(bus, host="0.0.0.0", port=8741)

# Client side (any machine)
client = MansioClient("http://mansio:8741", "coder-1", secret="sk-xxx")
```

- Suitable for multi-machine deployment, cloud environments
- Authentication mandatory
- Can pair with Redis/RabbitMQ backend for high availability

---

## 7. Configuration

### 7.1 Connection String (Current)

The Client SDK selects backends via connection strings, integrating configuration into code:

```python
MansioClient("mansio.db", agent_id)           # SQLite
MansioClient(":memory:", agent_id)             # Memory
MansioClient("redis://host:6379", agent_id)    # Redis
MansioClient("amqp://host:5672", agent_id)     # RabbitMQ
MansioClient("http://host:8741", agent_id)     # Remote service
```

### 7.2 Configuration File (Future, for MansioServer deployment)

Server-side deployment will support YAML/TOML configuration files:

```yaml
# mansio.yaml (reserved design, not yet implemented)
server:
  host: 0.0.0.0
  port: 8741
  require_auth: true

backend:
  url: redis://localhost:6379
  # url format follows connection string convention

serializer:
  type: json  # json | msgpack

logging:
  level: info
```

Configuration files ultimately resolve to connection strings + constructor parameters; both approaches are equivalent.

---

## 8. Error Handling

**MVP Strategy**: Simple retry + return error to agent; agent decides how to handle.

**Future Extensions**:
- Dead Letter Queue (DLQ)
- Configurable retry strategies
- Message delivery acknowledgment

---

## 9. Extension Roadmap

| Feature | Description | Dependency |
|---------|-------------|------------|
| Secret Rotation | rotate_secret() / revoke() | Client SDK |
| Message TTL | Per-channel-type expiration policies | Backend |
| Message Tracing | Distributed trace IDs | Message metadata |
| Priority Queue | Urgent message queue jumping | Backend |
| Delayed Messages | Scheduled delivery | Backend |
| Moderator Agent | Broadcast review mechanism | Client SDK |
| Async API | asyncio support | Full stack |
| Message Interruption | interrupt:{agent_id} channel + priority | Agent SDK layer |
| Per-channel Aliases | Similar to WeChat group cards | Client SDK |
| Agent Heartbeat | Liveness detection and expiration | Client SDK |

---

## 10. Decision Log

### D1: Backend and Storage Merged

**Decision**: Backend = transport + persistence combined; no separate Storage abstraction layer.

**Rationale**: All current backends (SQLite, Redis Streams, RabbitMQ) inherently include persistence. If a future pure-transport backend (e.g., MQTT) needs independent storage, it can compose internally without affecting the Protocol interface.

**Evolution Path**: When transport and persistence separation is genuinely needed (e.g., MQTT + PostgreSQL), an independent Storage Protocol can be introduced for internal composition within the Backend. The current Protocol interface requires no changes.

### D2: Channel Naming Enforced at Both Frontend and Client SDK Layers

**Decision**: Channel naming rules are validated at both the Frontend (server) layer and the Client SDK layer. The Bus layer accepts any channel name passed by its callers.

**Rationale**: Server-side validation in the Frontend prevents malformed channel names from reaching the Bus regardless of client implementation. Client-side validation in the SDK provides fast feedback. The Bus layer remains generic without embedding business semantics.

### D3: Identity Authentication via agent_id + secret

**Decision**: agent_id is user-chosen (format-constrained), secret is Mansio-generated, and authentication enforcement is controlled via Bus configuration.

**Rationale**: Simple, mature credential pattern supporting cross-session recovery (reconnect with same agent_id + secret). No-auth mode lowers the development/testing barrier.

### D4: Registry Stored in _system:registry Channel

**Decision**: Agent registration information is stored as messages in a system channel, not as a new table in the Backend layer.

**Rationale**: Follows the "everything is a message" principle without modifying the Backend Protocol interface. The Client SDK reads the channel to reconstruct agent state.

### D5: Cursor Persistence in _system Channel

**Decision**: Cursor snapshots are stored in the `_system:cursors:{agent_id}` channel.

**Rationale**: Reuses the message storage mechanism; cross-session recovery reads the latest snapshot from the channel. No additional state storage infrastructure needed.

### D6: Connection String Driven Deployment

**Decision**: MansioClient constructor accepts `Bus | str`, automatically selecting backend and transport based on URL scheme.

**Rationale**: Decouples deployment decisions (which backend, local vs. remote) from architectural design. The same Client code adapts to different deployment environments without modification.

### D7: API Adopts resource_action Naming

**Decision**: SDK method names use `resource_action` style (e.g., `channel_send`, `note_write`), simultaneously serving as MCP/CLI tool names.

**Rationale**: Resource + action naming provides the clearest semantics for LLM tool calling, and flat naming is well-suited for CLI subcommands and MCP tool names.

### D8: Semantic API is Sugar over Channel Operations

**Decision**: `note_write`, `thought_record`, `memory_store` and other semantic methods map to `channel_send` + specific channel + msg_type underneath.

**Rationale**: Keeps the system core minimal (everything is a message); high-level semantics are provided as convenience wrappers by the Client SDK. Users can also use channel operations directly for custom logic.
