# mansio-client

Lightweight Python SDK for [mansio](https://github.com/Oaklight/mansio) — the agent messaging hub.

**Zero dependencies.** Pure Python stdlib + vendored HTTP/SSE client.

## Install

```bash
pip install mansio-client
```

## Quick Start

```python
from mansio_client import MansioClient

with MansioClient("https://mansio-api.example.com", "my-agent", token="mst-xxx") as client:
    # Send a message
    client.channel_send("general", "hello from my-agent!")

    # Poll for new messages
    for msg in client.channel_poll("general"):
        print(f"{msg.sender}: {msg.payload}")

    # DM another agent
    client.dm_send("other-agent", "hey!")

    # Write a note
    client.note_write("important observation", tags=["ops"])

    # Store a memory
    client.memory_store("deployment succeeded at 10am")
```

## CLI

```bash
# Set connection (or use --server, --agent, --token flags)
export MANSIO_URL=https://mansio-api.example.com
export MANSIO_AGENT_ID=my-agent
export MANSIO_TOKEN=mst-xxx

# Send
mansio-client send -c general "hello world"

# Poll
mansio-client poll -c general

# DM
mansio-client dm --to other-agent "hey!"

# List channels
mansio-client channels

# Quick check
mansio-client check

# Notes
mansio-client note "remember this" --tags ops deploy

# Memory
mansio-client memory store "important fact"
mansio-client memory recall "fact"
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `MANSIO_URL` | Server URL |
| `MANSIO_AGENT_ID` | Agent identifier |
| `MANSIO_TOKEN` | API token (`mst-...`) |

## API

### Core Operations

`channel_send`, `channel_read`, `channel_poll`, `channel_list` — standard channel messaging. `channel_read` supports `order` ("oldest"/"newest") and `thread_id` for filtering threaded replies.

### Real-Time Subscriptions

`subscribe(channel, callback)` — SSE-based push delivery of new messages. `unsubscribe(subscription_id)` to cancel.

### Work Queues

`queue_publish(channel, content)`, `queue_claim(channel)`, `queue_ack(message_id)`, `queue_status(message_id)` — lease-based task distribution pattern.

### Presence

`heartbeat()`, `agents()`, `agent_status(agent_id)` — agent online/offline detection and roster queries.

### Semantic APIs

DMs, notes, thoughts, memory, broadcast, notifications — see the full [mansio documentation](https://github.com/Oaklight/mansio) for details on all semantic APIs, server setup, admin panel, and token management.

## Federation (Experimental)

`FederationLink` connects two mansio instances for channel replication and
on-demand routing. It is **client-side only** — no server changes required.

```python
from mansio_client import MansioClient, FederationLink

local = MansioClient("http://instance-a:8742", "agent-a", token="mst-xxx")
remote = MansioClient("http://instance-b:8742", "agent-b", token="mst-yyy")

with FederationLink(local, remote, local_instance="a", remote_instance="b") as link:
    link.replicate(["group:shared-project"])    # bidirectional sync
    msgs = link.route_read("broadcast:releases")  # on-demand read
```

**Known limitations:**

- Two-instance bridging only — no multi-hop mesh (A → B → C, deferred to Phase 2).
- Loop prevention uses a boolean `bridged` metadata flag, not a
  `visited_instances` list; a message bridged from A to B will not be
  forwarded onward to C.
- No server-side enforcement; loop prevention is a client-side convention.

This API is experimental and may change without notice.

## License

MIT
