# Remote Transport

Mansio supports remote agent communication via HTTP REST API + Server-Sent Events (SSE) push notifications.

## Architecture

```
Remote Agent ←→ HttpTransport ←→ (HTTP) ←→ HttpFrontend ←→ Bus ←→ Backend
```

## Server Setup

Start a server with HttpFrontend:

```python
from mansio import Bus, SQLiteBackend, MansioServer
from mansio.frontends import HttpFrontend

bus = Bus(backend=SQLiteBackend("mansio.db"))
server = MansioServer(bus)
server.add_frontend(HttpFrontend(host="0.0.0.0", port=8742))
server.serve_forever()
```

Or via CLI:

```bash
mansio serve --http 0.0.0.0:8742
```

## Client Connection

```python
from mansio import MansioClient

# URL-based connection automatically uses HttpTransport
client = MansioClient("http://server:8742", "my-agent")
client.channel_send("tasks", "hello from remote")
client.close()
```

## HTTP API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/publish` | Publish a message |
| `GET` | `/v1/query` | Query messages from a channel |
| `GET` | `/v1/channels` | List all channels |
| `GET` | `/v1/subscribe` | SSE stream for real-time notifications |
| `GET` | `/v1/auth/check` | Check auth requirements |
| `GET` | `/v1/registry/lookup` | Look up agent registration |
| `GET` | `/health` | Health check |

## SSE Push Notifications

Subscribe to real-time notifications:

```python
from mansio.transport_http import HttpTransport

transport = HttpTransport("http://server:8742", agent_id="listener")

def on_message(msg):
    print(f"{msg.sender}: {msg.payload}")

transport.subscribe("tasks", on_message)
# Messages arrive in background thread
```

Or via CLI:

```bash
mansio client poll -s http://server:8742 -a listener -c tasks --follow
```
