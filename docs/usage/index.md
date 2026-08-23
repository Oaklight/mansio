# Usage Guide

Learn how to use mansio for multi-agent messaging.

## Architecture

mansio uses a pluggable backend architecture defined by protocols in `mansio.protocols`:

- **`Backend`** -- handles message transport and persistence (e.g., `SQLiteBackend`, `MemoryBackend`)
- **`Serializer`** -- encodes/decodes metadata dicts (e.g., `JSONSerializer`)
- **`MessageBus`** -- the full bus interface (publish, poll, subscribe)

The `Bus` class composes a `Backend` with in-process pub/sub:

```python
from mansio import Bus, SQLiteBackend, MemoryBackend

# SQLite-backed (default)
bus = Bus(backend=SQLiteBackend("workspace/.mansio.db"))

# In-memory for testing
bus = Bus(backend=MemoryBackend())

# Shorthand for SQLite
from mansio import SQLiteBus
bus = SQLiteBus("workspace/.mansio.db")
```

The `backends/` package ships two implementations:

| Backend          | Use Case                                |
| ---------------- | --------------------------------------- |
| `SQLiteBackend`  | Production -- persistent, cross-process |
| `MemoryBackend`  | Testing -- fast, ephemeral              |

## Admin Panel

mansio includes a built-in admin panel for monitoring your message bus. Start it with:

```python
info = bus.start_admin()
print(f"Admin panel: {info.url}")  # http://127.0.0.1:8741
```

The admin panel provides four tabs:

- **Dashboard** -- aggregate stats and throughput chart
- **Channels** -- per-channel breakdown with message counts and subscription info
- **Messages** -- browse and filter messages by channel, sender, or type; publish new messages
- **Subscriptions** -- view active in-process subscriptions

For remote access, enable authentication:

```python
info = bus.start_admin(remote=True)
# auth_token is auto-generated; pass it via Authorization header
```

Stop the admin panel with `bus.stop_admin()` or `bus.close()`.
