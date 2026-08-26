# Mansio

[![CI](https://github.com/Oaklight/mansio/actions/workflows/ci.yml/badge.svg)](https://github.com/Oaklight/mansio/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mansio?color=%23800020&label=PyPI)](https://pypi.org/project/mansio/)
[![Release](https://img.shields.io/github/v/release/Oaklight/mansio?color=%23800020&label=Release)](https://github.com/Oaklight/mansio/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

English Version | [中文版](README_zh.md)

A lightweight message bus for multi-agent AI collaboration — the relay station (驿站) where agents meet.

## Overview

Mansio provides structured, persistent communication channels for AI agents. Instead of point-to-point RPC or shared memory, agents interact through named channels with pub/sub semantics, cursor-based polling, and built-in identity management.

```
Backend (storage)  →  Bus (routing)  →  Client SDK (agent API)
   SQLite / Memory       pub/sub           identity, cursors,
                         channels           DMs, notes, memory
```

## Features

- **Channel-based messaging** — named channels with pub/sub, cursor-tracked polling, and message ordering via monotonic UUIDs
- **Server-side validation** — channel names, message payloads, and query parameters are validated at the Frontend layer; malformed input is rejected before reaching the Bus
- **Access control** — system channels (`_system:*`), private channels (`notebook:X`, `memory:X`), and broadcast channels enforce per-agent write restrictions; supertokens grant elevated access
- **Pluggable storage** — `SQLiteBackend` (persistent, WAL mode) and `MemoryBackend` (ephemeral, testing); protocol-based, easy to extend
- **Client SDK** — `MansioClient` with agent identity, cursor persistence across sessions, and token-based authentication (per-agent secrets and supertokens)
- **Semantic APIs** — DMs, broadcast channels, notes (with tags), thoughts (chain-of-thought logging), memory (store/recall), notifications
- **Admin panel** — built-in HTTP dashboard with REST API for stats, channel browsing, message inspection, and throughput monitoring; modular `admin/routes/` subpackage with dict-based dispatch
- **Flexible connection** — connect via Bus object, file path (SQLite), or `:memory:` string; URL schemes (`http://`, `redis://`) reserved for future transports
- **MCP server** — `mansio mcp-serve` exposes all client operations as MCP tools over JSON-RPC stdio, enabling any MCP-capable agent to interact with mansio
- **Real-time subscriptions** — SSE-based `subscribe(channel, callback)` for push-style message delivery
- **Message threading** — `parent_id` and `thread_id` fields for reply chains and conversation context
- **Work queues** — `queue_publish`, `queue_claim`, `queue_ack` pattern for task distribution with lease-based claiming
- **Agent presence** — `heartbeat()`, `agents()`, `agent_status()` for online/offline detection
- **Push integration** — three-tier approach (MCP tools → framework adapters → prompt instructions) with per-framework examples in `examples/`
- **NATS backend** — distributed messaging via NATS JetStream (optional `nats` extra)
- **Zero runtime dependencies** — pure Python, stdlib only

## Quick Start

```python
from mansio import MansioClient

# In-memory bus (for testing)
with MansioClient(":memory:", "agent-alpha") as alice:
    alice.channel_send("general", "hello everyone!")
    alice.note_write("remember to check logs", tags=["ops"])
    alice.thought_record("planning", "next steps", "need to coordinate with bob")

# SQLite-backed (persistent)
with MansioClient("/tmp/mansio.db", "agent-alpha") as alice:
    alice.dm_send("agent-beta", "ready to sync?")

# Multi-agent collaboration
from mansio import Bus, MemoryBackend

bus = Bus(backend=MemoryBackend())

alice = MansioClient(bus, "agent-alice")
bob = MansioClient(bus, "agent-bob")

alice.dm_send("agent-bob", "PR is ready for review")
messages = bob.dm_read("agent-alice")  # ["PR is ready for review"]

alice.close()
bob.close()
bus.close()
```

## Architecture

Mansio follows a layered architecture inspired by messaging middleware, adapted for AI agent workflows:

| Layer | Component | Role |
|-------|-----------|------|
| **Storage** | `Backend` protocol | Persistent or ephemeral message storage (`SQLiteBackend`, `MemoryBackend`) |
| **Routing** | `Bus` | Channel management, pub/sub dispatch, UUID generation |
| **Transport** | `Transport` protocol | Abstraction for local vs. remote bus access (Bus directly satisfies Transport) |
| **Agent API** | `MansioClient` | Identity, cursors, auth, semantic messaging APIs |
| **Frontend** | `Frontend` protocol | Network-facing servers (REST + SSE) binding to Bus (`HttpFrontend`, `MansioServer`) |
| **Admin** | `AdminServer` | HTTP dashboard + REST API for monitoring |

For detailed design rationale, see [DESIGN_EN.md](docs/DESIGN_EN.md).

## Installation

Requires **Python >= 3.10**.

```bash
pip install mansio
```

Or from source:

```bash
git clone https://github.com/Oaklight/mansio.git
cd mansio
pip install -e ".[dev]"
```

## Client SDK API

### Core Operations

| Method | Description |
|--------|-------------|
| `channel_send(channel, content)` | Send message to a channel |
| `channel_read(channel)` | Read messages (no cursor advance); supports `order` ("oldest"/"newest") and `thread_id` params |
| `channel_poll(channel)` | Poll new messages (advances cursor) |
| `channel_list()` | List all channels; supports `detail=True` for metadata |

### Semantic APIs

| Method | Description |
|--------|-------------|
| `dm_send(target, content)` | Send direct message |
| `dm_read(peer)` | Read DM conversation |
| `note_write(content, tags=)` | Write a note with optional tags |
| `note_read(tags=)` | Read notes, optionally filtered by tags |
| `thought_record(mode, focus, content)` | Record chain-of-thought |
| `thought_read()` | Read thought history |
| `memory_store(content)` | Store a memory |
| `memory_recall(query)` | Recall memories by keyword |
| `broadcast_list()` / `broadcast_read(topic)` | Browse broadcast channels |
| `notification_check()` | Poll notifications |

### Real-Time

| Method | Description |
|--------|-------------|
| `subscribe(channel, callback)` | Subscribe to real-time messages via SSE |
| `unsubscribe(subscription_id)` | Cancel a subscription |

### Work Queue

| Method | Description |
|--------|-------------|
| `queue_publish(channel, content)` | Publish a task to a work queue |
| `queue_claim(channel)` | Claim the next available task (with lease) |
| `queue_ack(message_id)` | Acknowledge task completion |
| `queue_status(message_id)` | Check task claim status |

### Presence

| Method | Description |
|--------|-------------|
| `heartbeat()` | Send presence heartbeat |
| `agents()` | List agents with presence info |
| `agent_status(agent_id)` | Check specific agent's presence |

### Authentication

```python
# Register new agent (returns client + secret)
client, secret = MansioClient.register(bus, "agent-id")

# Reconnect with secret
client = MansioClient(bus, "agent-id", secret=saved_secret)
```

### Admin Panel

```python
from mansio import SQLiteBus

bus = SQLiteBus("mansio.db")
info = bus.start_admin(port=8741)
print(f"Dashboard: {info.url}")
# Visit http://localhost:8741 for the web UI
```

### MCP Server

```bash
mansio mcp-serve --url http://localhost:8742 --agent-id my-agent --token mst-xxx
```

Exposes all `MansioClient` operations as MCP tools over JSON-RPC stdio. Compatible with Claude Code, Codex, and any MCP-capable agent framework. See `examples/adapters/` for per-framework setup guides.

## Roadmap

### Shipped

- [x] **RemoteTransport** — `HttpFrontend`, `MansioServer`, `HttpTransport`
- [x] **IRC Frontend** — shipped as optional `irc` extra
- [x] **Channel ACL** — system, private, and broadcast channel write restrictions enforced server-side
- [x] **Message Threading** — `parent_id` / `thread_id` nested reply support
- [x] **Message Deletion** — single and per-channel deletion, admin bulk cleanup
- [x] **Pagination** — offset-based pagination with `total`, `has_more`, `offset` metadata
- [x] **MCP Server** — Model Context Protocol integration (`mansio[mcp]`)
- [x] **Presence & Heartbeat** — agent online/offline status and live subscriptions
- [x] **NATS Backend** — JetStream-based persistent storage
- [x] **Maildir Backend** — filesystem-based storage
- [x] **Compaction** — registry and cursor compaction for long-running instances
- [x] **Remote Transport Reliability** — SSE reconnect (Last-Event-ID), WAL retry logging, slow-consumer drop notification
- [x] **Work Queues** — publish/claim/ack with lease-based task distribution
- [x] **Push Integration** — MCP tools + framework adapters + polling templates
- [x] **Client-side Injection** — per-framework message injection adapters

### Planned
- [ ] **Message TTL** — automatic expiry and cleanup
- [ ] **Async API** — native async/await support
- [ ] **Semantic memory recall** — vector embedding search
- [ ] **Redis/AMQP backends** — distributed storage
- [ ] **Federation** — cross-instance communication ([#4](https://github.com/Oaklight/mansio/issues/4))

## Academic Context

Mansio is the reference implementation for Chapter 9 of a dissertation on enabling agentic AI at scale through decoupled abstractions. The design emphasizes protocol-based interfaces, pluggable components, and a clear separation between transport, storage, and agent-level semantics.

## License

MIT — see [LICENSE](LICENSE) for details.
