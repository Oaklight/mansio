---
hide:
  - navigation
---
# mansio

**Agent messaging hub for multi-agent collaboration.**

## Overview

mansio is a lightweight, zero-dependency message bus for multi-agent systems. Agents communicate through channels using a simple publish/subscribe model, with pluggable backends for storage and frontends for network access.

## Features

- **Zero runtime dependencies** — pure Python stdlib
- **Three-layer architecture** — Backend (storage) ↔ Bus (routing) ↔ Frontend (access protocol)
- **Multiple backends** — SQLite (persistent) and Memory (ephemeral)
- **Remote transport** — HTTP REST API + SSE push notifications
- **IRC frontend** — bridge agent communication to IRC channels
- **Admin panel** — built-in web dashboard for monitoring
- **CLI** — `mansio serve` for server, `mansio client` for agent operations
- **MansioClient SDK** — identity management, cursor tracking, DMs, notes, memory

## Quick Example

```python
from mansio import SQLiteBus, MansioClient

bus = SQLiteBus("mansio.db")

client = MansioClient(bus, "my-agent")
client.channel_send("tasks", "hello world")

msgs = client.channel_poll("tasks")
print(msgs[0].payload)  # "hello world"

client.close()
bus.close()
```

## Architecture

```
Agent ←→ MansioClient ←→ Transport ←→ Bus ←→ Backend
                              ↑
                        HttpFrontend / IrcFrontend
                              ↑
                        Remote Agents
```

## Installation

```bash
pip install mansio
```

See the [Installation Guide](usage/installation.md) for details, or jump to the [Quick Start](usage/quickstart.md).
