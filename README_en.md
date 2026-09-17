# Mansio

[![CI](https://github.com/Oaklight/mansio/actions/workflows/ci.yml/badge.svg)](https://github.com/Oaklight/mansio/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mansio?color=%23800020&label=PyPI)](https://pypi.org/project/mansio/)
[![Release](https://img.shields.io/github/v/release/Oaklight/mansio?color=%23800020&label=Release)](https://github.com/Oaklight/mansio/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

English Version | [中文版](README_zh.md)

A lightweight message bus for multi-agent AI collaboration — the relay station (驿站) where agents meet.

## Overview

Mansio provides structured, persistent communication channels for AI agents. Instead of point-to-point RPC or shared memory, agents interact through named channels with pub/sub semantics, cursor-based polling, and built-in identity management.

Mansio ships as two packages:

| Package | Import name | Role |
|---------|-------------|------|
| `mansio` | `mansio` | The server: storage backends, Bus, HTTP/IRC frontends, admin panel, the `mansio` CLI |
| `mansio-client` | `mansio_client` | The agent SDK: `MansioClient`, the `mansio-client` CLI, the `mansio-mcp` MCP server |

Agents always talk to a running server over HTTP:

```
Backend (storage)  →  Bus (routing)  →  Frontend (network)  →  mansio-client
 SQLite / Memory /       pub/sub,         HTTP (REST+SSE),      identity, cursors,
 NATS / Maildir          channels         IRC                   DMs, notes, memory
```

## Features

- **Channel-based messaging** — named channels with pub/sub, cursor-tracked polling, and message ordering via time-ordered UUIDs (UUID v7)
- **Server-side validation** — channel names, message payloads, and query parameters are validated at the Frontend layer; malformed input is rejected before reaching the Bus
- **Access control** — system channels (`_system:*`), private channels (`notebook:X`, `memory:X`), and broadcast channels enforce per-agent write restrictions; supertokens grant elevated access
- **Pluggable storage** — `SQLiteBackend` (persistent, WAL mode), `MemoryBackend` (ephemeral, testing), `NATSBackend` (JetStream), `MaildirBackend` (filesystem); protocol-based, easy to extend
- **Client SDK** — `mansio_client.MansioClient` with agent identity, cursor persistence across sessions, and bearer-token authentication
- **Semantic APIs** — DMs, broadcast channels, notes (with tags), thoughts (chain-of-thought logging), memory (store/recall), notifications
- **Admin panel** — built-in HTTP dashboard with REST API for stats, channel browsing, message inspection, throughput monitoring, and agent/token management
- **MCP server** — `mansio-mcp` exposes the client operations as MCP tools over JSON-RPC stdio, enabling any MCP-capable agent to interact with mansio
- **Real-time subscriptions** — SSE-based `subscribe(channel, callback)` for push-style message delivery
- **Message threading** — `parent_id` and `thread_id` fields for reply chains and conversation context
- **Work queues** — `queue_publish`, `queue_claim`, `queue_ack` pattern for task distribution with lease-based claiming
- **Agent presence** — `heartbeat()`, `users()`, `user_status()` for online/offline detection
- **Push integration** — three-tier approach (MCP tools → framework adapters → prompt instructions) with per-framework examples in `examples/`
- **Zero runtime dependencies** — pure Python, stdlib only (optional extras pull in `irc` and `nats-py`)

## Quick Start

Start a server (development mode, no authentication):

```bash
mansio serve --db mansio.db --http 8742 --admin-port 8741 --no-auth
```

Then connect agents to it:

```python
from mansio_client import MansioClient

URL = "http://localhost:8742"

with MansioClient(URL, "agent-alice") as alice:
    alice.channel_send("general", "hello everyone!")
    alice.note_write("remember to check logs", tags=["ops"])
    alice.thought_record(
        "need to coordinate with bob",
        thinking_mode="planning",
        focus_area="next steps",
    )
    alice.dm_send("agent-bob", "PR is ready for review")

with MansioClient(URL, "agent-bob") as bob:
    for msg in bob.channel_poll("general"):
        print(f"{msg.sender}: {msg.payload}")
    for msg in bob.dm_read("agent-alice"):
        print(f"DM from {msg.sender}: {msg.payload}")
```

```
agent-alice: hello everyone!
DM from agent-alice: PR is ready for review
```

The same operations are available from the shell:

```bash
export MANSIO_URL=http://localhost:8742
export MANSIO_USER_ID=agent-alice

mansio-client send -c general "hello from the CLI"
mansio-client read -c general -n 5
mansio-client dm --to agent-bob "hey!"
```

## Architecture

Mansio follows a layered architecture inspired by messaging middleware, adapted for AI agent workflows:

| Layer | Component | Role |
|-------|-----------|------|
| **Storage** | `Backend` ABC | Persistent or ephemeral message storage (`SQLiteBackend`, `MemoryBackend`, `NATSBackend`, `MaildirBackend`) |
| **Routing** | `Bus` | Channel management, pub/sub dispatch, UUID generation, compaction policy |
| **Frontend** | `Frontend` protocol | Network-facing servers binding to a Bus (`HttpFrontend` for REST + SSE, `IrcFrontend`) |
| **Orchestration** | `MansioServer` | Binds one Bus to one or more Frontends |
| **Admin** | `AdminServer` | HTTP dashboard + REST API for monitoring and token management |
| **Agent API** | `mansio_client.MansioClient` | Identity, cursors, auth, semantic messaging APIs over HTTP |

For detailed design rationale, see [DESIGN_EN.md](docs/DESIGN_EN.md).

## Installation

Requires **Python >= 3.10**.

```bash
pip install mansio         # server
pip install mansio-client  # agent SDK, CLI and MCP server
```

Or from source — the two packages live in subdirectories, so install both:

```bash
git clone https://github.com/Oaklight/mansio.git
cd mansio
pip install -e "mansio[dev,test]" -e mansio-client/
```

The `mansio` package provides these optional extras: `dev` (lint, type check, build), `test` (pytest), `irc` (IRC frontend), `nats` (NATS JetStream backend).

## Running a Server

```bash
mansio serve --db mansio.db --http 8742 --admin-port 8741
```

| Flag | Meaning |
|------|---------|
| `--db PATH` | SQLite database path (default: `mansio.db`) |
| `--maildir PATH` / `--nats URL` | Use the Maildir or NATS backend instead of SQLite |
| `--http [HOST:]PORT` | Enable the HTTP frontend (agents cannot connect without it) |
| `--admin-port PORT` | Admin panel port (default: 8741) |
| `--host ADDR` | Admin panel bind address (default: 127.0.0.1) |
| `--remote` | Bind the admin panel to 0.0.0.0 and auto-generate an admin password |
| `--no-auth` | Disable API token auth and admin auth — development only |
| `--irc HOST:PORT` | Bridge channels to an IRC server (requires the `irc` extra) |

Without `--no-auth`, the HTTP API requires a bearer token and rejects unauthenticated requests with `401`.

## Authentication

Agents authenticate with a bearer token (`mst-...`) issued by the server. Tokens are managed from the admin panel UI, or over its REST API:

```bash
curl -X POST http://localhost:8741/api/users \
     -H 'Content-Type: application/json' \
     -d '{"user_id": "agent-alice", "label": "quickstart"}'
```

```json
{"ok": true, "user_id": "agent-alice", "token": {"id": "584484b2", "token": "mst-7c3a...", "user_id": "agent-alice", "label": "quickstart"}}
```

Pass the token to the client:

```python
from mansio_client import MansioClient

with MansioClient("http://localhost:8742", "agent-alice", token="mst-...") as alice:
    alice.channel_send("general", "authenticated hello")
```

A token whose `user_id` is unset is a **supertoken**: it may act as any agent and may write to `broadcast:*` and `_system:*` channels. The admin API requires a `user_id`, so supertokens are created programmatically ([#299](https://github.com/Oaklight/mansio/issues/299)):

```python
from mansio.token_store import TokenStore

print(TokenStore("mansio.db").create_token(user_id=None, label="admin")["token"])
```

Writing to `broadcast:*` is restricted to supertokens unconditionally; per-channel ACL entries do not grant it ([#298](https://github.com/Oaklight/mansio/issues/298)).

## Admin Panel

`mansio serve` starts the admin panel alongside the bus. Visit `http://localhost:8741` for the web UI, which shows bus statistics, channel and message browsing, live throughput, and agent/token management. The same data is available as JSON under `/api/` (`/api/stats`, `/api/stats/throughput`, `/api/channels`, `/api/messages`, `/api/subscriptions`, `/api/users`, `/api/tokens`, …).

The panel binds to `127.0.0.1` and is unauthenticated by default; use `--host`/`--remote` together with `--admin-password` to expose it.

## Client SDK API

All methods below are on `mansio_client.MansioClient(url, user_id, *, token=None, display_name=None)`.

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
| `dm_send(to_user, content)` | Send direct message |
| `dm_read(with_user)` | Read DM conversation |
| `note_write(content, tags=)` | Write a note with optional tags |
| `note_read(tags=)` | Read notes, optionally filtered by tags |
| `thought_record(thought_process, thinking_mode=, focus_area=)` | Record chain-of-thought |
| `thought_read()` | Read thought history |
| `memory_store(content, memory_type=)` | Store a memory |
| `memory_recall(query)` | Recall memories by keyword |
| `broadcast_list()` / `broadcast_read(topic)` | Browse broadcast channels |
| `notification_check()` | Poll notifications |

### Real-Time

| Method | Description |
|--------|-------------|
| `subscribe(channel, callback)` | Subscribe to real-time messages via SSE |
| `unsubscribe(subscription_id)` | Cancel a subscription |
| `listen(channels, injector)` | Subscribe to several channels, routing messages through an [injector](docs/injectors.md) |

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
| `users()` | List agents with presence info |
| `user_status(user_id)` | Check specific agent's presence |

### Channel Management

| Method | Description |
|--------|-------------|
| `channel_create(name, visibility=)` | Create a channel with metadata |
| `channel_delete(name)` | Delete a channel and its messages |
| `message_delete(message_id)` | Delete a single message |
| `acl_get` / `acl_set` / `acl_add` / `acl_remove` | Manage per-channel access control entries (these gate the ACL endpoints themselves; message access uses the structural channel rules — see [DESIGN_EN.md](docs/DESIGN_EN.md) §3.5.3) |
| `registry_lookup(user_id)` | Check whether an agent is registered |

## MCP Server

```bash
mansio-mcp --url http://localhost:8742 --user-id my-agent --token mst-xxx
```

Ships with `mansio-client`. Each flag falls back to the matching environment variable (`MANSIO_URL`, `MANSIO_USER_ID`, `MANSIO_TOKEN`, `MANSIO_DISPLAY_NAME`), so a framework that sets the environment can launch it with no arguments. Exposes the client operations as MCP tools over JSON-RPC stdio. Compatible with Claude Code, Codex, and any MCP-capable agent framework. See `examples/adapters/` for per-framework setup guides.

## Roadmap

### Shipped

- [x] **HTTP Frontend** — `HttpFrontend`, `MansioServer`, `HttpTransport`
- [x] **IRC Frontend** — shipped as optional `irc` extra
- [x] **Channel ACL** — system, private, and broadcast channel write restrictions enforced server-side
- [x] **Message Threading** — `parent_id` / `thread_id` nested reply support
- [x] **Message Deletion** — single and per-channel deletion, admin bulk cleanup
- [x] **Pagination** — offset-based pagination with `total`, `has_more`, `offset` metadata
- [x] **MCP Server** — Model Context Protocol integration (`mansio-mcp`, in `mansio-client`)
- [x] **Presence & Heartbeat** — agent online/offline status and live subscriptions
- [x] **NATS Backend** — JetStream-based persistent storage
- [x] **Maildir Backend** — filesystem-based storage
- [x] **Compaction** — registry and cursor compaction for long-running instances
- [x] **Remote Transport Reliability** — SSE reconnect (Last-Event-ID), WAL retry logging, slow-consumer drop notification
- [x] **Work Queues** — publish/claim/ack with lease-based task distribution
- [x] **Push Integration** — MCP tools + framework adapters + polling templates
- [x] **Client-side Injection** — per-framework message injection adapters
- [x] **Federation** — explicit two-instance replication and routing via `FederationLink`; supports bidirectional/pull/push channel sync and on-demand remote read/send ([#4](https://github.com/Oaklight/mansio/issues/4))

### Planned

- [ ] **Message TTL** — automatic expiry and cleanup
- [ ] **Async API** — native async/await support
- [ ] **Semantic memory recall** — vector embedding search
- [ ] **Redis/AMQP backends** — distributed storage
- [ ] **Federation v2** — `@instance` addressing, multi-hop mesh routing, instance discovery

## Academic Context

Mansio is the reference implementation for Chapter 9 of a dissertation on enabling agentic AI at scale through decoupled abstractions. The design emphasizes protocol-based interfaces, pluggable components, and a clear separation between transport, storage, and agent-level semantics.

## License

MIT — see [LICENSE](LICENSE) for details.
