# IRC Frontend

The IRC frontend bridges mansio channels to IRC channels, allowing agents and human operators to communicate via standard IRC clients.

## Installation

```bash
pip install mansio[irc]
```

## Usage

```python
from mansio import SQLiteBus
from mansio.frontends.irc import IrcFrontend

bus = SQLiteBus("mansio.db")

frontend = IrcFrontend(
    irc_host="irc.example.com",
    irc_port=6667,
    nickname="mansio-bot",
    channels=["tasks", "sync"],
)
frontend.attach(bus)
frontend.serve_forever()  # blocks
```

Or via CLI:

```bash
mansio serve --http 8742 \
    --irc irc.example.com:6667 \
    --irc-channels tasks sync \
    --irc-nick mansio-bot
```

## Channel Mapping

By default, mansio channel `tasks` maps to IRC channel `#tasks`. Use `channel_map` for explicit overrides:

```python
frontend = IrcFrontend(
    irc_host="irc.example.com",
    channels=["tasks"],
    channel_map={"tasks": "#project-tasks"},
)
```

## Message Flow

**IRC → mansio**: messages in mapped IRC channels are published to the corresponding mansio channel with `metadata={"source": "irc"}`.

**mansio → IRC**: messages published to mansio channels are forwarded to the mapped IRC channel as `sender: payload`. Messages originating from IRC are not echoed back.

## SSL/TLS

```python
frontend = IrcFrontend(
    irc_host="irc.libera.chat",
    irc_port=6697,
    use_ssl=True,
    channels=["tasks"],
)
```

Or via CLI:

```bash
mansio serve --irc irc.libera.chat:6697 --irc-ssl --irc-channels tasks
```
