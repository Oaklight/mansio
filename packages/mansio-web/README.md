# mansio

[![CI](https://github.com/Oaklight/mansio/actions/workflows/ci.yml/badge.svg)](https://github.com/Oaklight/mansio/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mansio?color=%23800020&label=PyPI)](https://pypi.org/project/mansio/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

Server side of [mansio](https://github.com/Oaklight/mansio) — a lightweight message bus for multi-agent AI collaboration.

**No third-party runtime dependencies.** Pure Python stdlib, a vendored HTTP
server, and the internal `mansio-core` package.

This package contains the network frontends, the admin panel, the server
orchestration and the `mansio` CLI. The types, Bus, storage backends and
authorization live in the `mansio-core` dependency. Agents connect with the
separate [`mansio-client`](https://pypi.org/project/mansio-client/) SDK.

## Install

```bash
pip install mansio                 # server + core (SQLite / Memory / Maildir)
pip install "mansio-core[nats]"    # + NATS JetStream backend
pip install "mansio[irc]"          # + IRC frontend
```

## Run a Server

```bash
mansio serve --db mansio.db --http 8742 --admin-port 8741
```

- `--http [HOST:]PORT` enables the HTTP frontend (REST + SSE). Agents cannot connect without it.
- `--admin-port PORT` serves the admin dashboard and its REST API (default: 8741).
- `--maildir PATH` / `--nats URL` select a different backend.
- `--no-auth` disables API and admin authentication — development only.

Without `--no-auth` the HTTP API requires a bearer token; issue one from the admin panel or its API:

```bash
curl -X POST http://localhost:8741/api/users \
     -H 'Content-Type: application/json' \
     -d '{"user_id": "agent-alice", "label": "initial token"}'
```

## Embedding

```python
from mansio import Bus, SQLiteBackend, MansioServer
from mansio.frontends import HttpFrontend

bus = Bus(backend=SQLiteBackend("mansio.db"))
server = MansioServer(bus)
server.add_frontend(HttpFrontend(host="127.0.0.1", port=8742))
server.serve_forever()  # blocks
```

## Components

| Layer | Component |
|-------|-----------|
| Storage | `SQLiteBackend`, `MemoryBackend`, `NATSBackend`, `MaildirBackend` (`Backend` ABC, plus the optional `ChannelStore`, `Deletable`, `Presenceable`, `Compactable` protocols) |
| Routing | `Bus`, `SQLiteBus` |
| Frontend | `HttpFrontend` (REST + SSE), `IrcFrontend` |
| Orchestration | `MansioServer` |
| Admin | `AdminServer`, `TokenStore` |

## CLI

`mansio` ships `mansio client` for smoke-testing a server: `send`, `poll`,
`channels` and `dm`. It is the minimal subset of the separate
[`mansio-client`](https://pypi.org/project/mansio-client/) CLI, which is the
full SDK-over-CLI for agent work. Both accept `--server` / `--user-id` /
`--token` and fall back to `MANSIO_URL` / `MANSIO_USER_ID` / `MANSIO_TOKEN`
(converged in #339); their subcommand sets differ on purpose.

## Documentation

Full documentation, the client API reference and the design document live in the
[repository README](https://github.com/Oaklight/mansio#readme) and
[docs/DESIGN_EN.md](https://github.com/Oaklight/mansio/blob/master/docs/DESIGN_EN.md).

## License

MIT
