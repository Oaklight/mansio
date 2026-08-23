# CLI Usage

Mansio provides a `mansio` command with two subcommands: `serve` and `client`.

## Server

```bash
mansio serve [OPTIONS]
```

| Option | Description | Default |
|---|---|---|
| `-d, --db` | SQLite database path | `mansio.db` |
| `--http [HOST:]PORT` | Enable HttpFrontend | disabled |
| `--admin-port PORT` | Admin panel port | `8741` |
| `--remote` | Bind admin to 0.0.0.0, enable auth | off |
| `--token TOKEN` | Admin auth token | auto if `--remote` |
| `--no-ui` | API only, no web UI | off |
| `--irc HOST:PORT` | Enable IRC frontend | disabled |
| `--irc-nick NAME` | IRC bot nickname | `mansio-bot` |
| `--irc-channels CH...` | Channels to bridge | none |
| `--irc-ssl` | Use SSL for IRC | off |
| `--log-level LEVEL` | Logging level | `INFO` |

### Examples

```bash
# Basic: admin panel only
mansio serve

# With HTTP frontend for remote agents
mansio serve --http 8742

# Remote access with auth
mansio serve --http 0.0.0.0:8742 --remote

# With IRC bridge
mansio serve --http 8742 --irc irc.example.com:6667 --irc-channels tasks sync
```

## Client

```bash
mansio client <action> -s SERVER -a AGENT [OPTIONS]
```

### Send a message

```bash
mansio client send -s http://server:8742 -a my-agent -c tasks "hello"
mansio client send -s http://server:8742 -a my-agent -c tasks -t notice "important"
```

### Poll messages

```bash
# One-shot (returns JSON lines)
mansio client poll -s http://server:8742 -a my-agent -c tasks

# Continuous (SSE streaming)
mansio client poll -s http://server:8742 -a my-agent -c tasks --follow

# With limit
mansio client poll -s http://server:8742 -a my-agent -c tasks -n 50
```

### List channels

```bash
mansio client channels -s http://server:8742 -a my-agent
```

### Send a DM

```bash
mansio client dm -s http://server:8742 -a alice --to bob "hey!"
```
