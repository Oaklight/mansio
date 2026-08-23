---
hide:
  - navigation
---
# Examples

## Basic Usage

```python
from mansio import SQLiteBus

with SQLiteBus("chat.db") as bus:
    bus.publish("general", "agent-a", "text", "Hello!")
    messages = bus.poll("general")
    for msg in messages:
        print(f"{msg.sender}: {msg.payload}")
```

## Using MemoryBackend for Testing

```python
from mansio import Bus, MemoryBackend

with Bus(backend=MemoryBackend()) as bus:
    bus.publish("test-ch", "test-agent", "text", "test message")
    assert len(bus.poll("test-ch")) == 1
```

## Custom Backend

```python
from mansio import Bus
from mansio.protocols import Backend
from mansio.types import Message

class MyCustomBackend:
    """Implement the Backend protocol."""

    def store(self, message: Message) -> None: ...
    def query(self, channel: str, after: str | None = None, limit: int = 100) -> list[Message]: ...
    def list_channels(self) -> list[str]: ...
    def close(self) -> None: ...
    def count_messages(self, channel: str | None = None) -> int: ...
    def query_all(self, after=None, limit=100, channel=None, sender=None, msg_type=None) -> list[Message]: ...
    def get_stats(self) -> dict: ...
    def query_recent_timestamps(self, seconds: int = 60) -> list[str]: ...

bus = Bus(backend=MyCustomBackend())
```

## Starting the Admin Panel

```python
from mansio import SQLiteBus

bus = SQLiteBus("workspace/.mansio.db")

# Start the admin panel (runs in a background thread)
info = bus.start_admin()
print(f"Admin panel: {info.url}")

# ... use the bus normally ...

# Stop the admin panel when done
bus.stop_admin()
```

For remote access with authentication:

```python
info = bus.start_admin(remote=True)
print(f"Admin panel: {info.url}")
print(f"Auth token: {info.token}")  # auto-generated token
```
