# Admin Panel

The built-in admin panel provides a web dashboard and REST API for monitoring and interacting with the message bus.

## Starting the Admin Panel

```python
from mansio import SQLiteBus

bus = SQLiteBus("mansio.db")
info = bus.start_admin(port=8741)
print(f"Dashboard: {info.url}")
```

Or via CLI:

```bash
mansio serve --admin-port 8741
```

## REST API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/stats` | Dashboard statistics |
| `GET` | `/api/stats/throughput` | Per-second message throughput |
| `GET` | `/api/channels` | List all channels |
| `GET` | `/api/channels/{name}` | Channel details |
| `GET` | `/api/messages?channel=...` | Browse messages |
| `POST` | `/api/messages` | Publish a message |
| `GET` | `/api/subscriptions` | Active subscriptions |

## Remote Access

Enable remote access with authentication:

```bash
mansio serve --remote --token my-secret-token
```

The admin panel will bind to `0.0.0.0` and require the token for all API requests.
