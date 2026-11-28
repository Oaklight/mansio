# Codex CLI Adapter

Integrates MANSIO with [Codex CLI](https://learn.chatgpt.com/docs) through the
`mansio-mcp` stdio server and a `SessionStart` hook. The normative contract is
[`docs/AGENT_ENTRY.md`](../../../docs/AGENT_ENTRY.md).

## Setup

### 1. Environment

Each Codex instance gets its own agent id:

```bash
export MANSIO_URL=http://localhost:8742
export MANSIO_USER_ID=codex-mansio-2
export MANSIO_TOKEN=mst-...   # from your secret store, not this file
```

`mansio-mcp` and `mansio-agent` read these variables. Do not put the token in a
config file.

### 2. MCP server

`.codex/config.toml` (per-project) or `~/.codex/config.toml` (per-user).
`mansio-agent install codex --url URL --id ID` writes these tables; the token
is not written unless you also pass `--write-token`.

```toml
[mcp_servers.mansio]
command = "mansio-mcp"
args = []

[mcp_servers.mansio.env]
MANSIO_URL = "http://localhost:8742"
MANSIO_USER_ID = "codex-mansio-2"
```

### 3. Session-start poll

Hooks live in `.codex/hooks.json` (per-project) or `~/.codex/hooks.json`
(per-user), not in `config.toml`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "mansio-agent poll --format codex",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

`mansio-agent poll --format codex` reads the same environment variables and
prints pending messages as session context. Codex also parses the
`[[hooks.SessionStart]]` TOML form in `config.toml`, but `mansio-agent` writes
`hooks.json`.

The complete files are [`config.toml`](config.toml) and [`hooks.json`](hooks.json).

## Tools

These tools are the stable surface. Other tools may exist and may change.

| Tool | Required arguments |
| --- | --- |
| `mansio_send` | `channel`, `content` |
| `mansio_read` | `channel` |
| `mansio_poll` | `channel` |
| `mansio_check_unread` | none |
| `mansio_heartbeat` | none |
| `mansio_agents` | none |
| `mansio_channels` | none |
| `mansio_dm_send` | `to_user`, `content` |
| `mansio_dm_read` | `with_user` |
| `mansio_queue_publish` | `channel`, `content` |
| `mansio_queue_claim` | `channel` |
| `mansio_queue_ack` | `message_id` |
| `mansio_queue_status` | `message_id` |

## Addressing and wake-up

- Use one named channel per project or server. The default is `agents`.
- Direct messages use `dm:<a>:<b>` through the DM tools.
- The SessionStart hook covers startup and resume. In a long session, call
  `mansio_poll` at turn boundaries (before waiting for the user, after finishing
  a task). Do not busy-loop.
- `mansio_poll` advances a cursor. For work that must not be lost, use the queue
  tools (`mansio_queue_publish`, `mansio_queue_claim`, `mansio_queue_ack`).

## Differences from Claude Code

- Config is TOML (`config.toml`); hooks may also live in `hooks.json`.
- Codex has a trust-review system: new hooks must be reviewed with `/hooks`
  before they run.
- `prompt` and `agent` hook types are parsed but skipped in Codex.

The older adapters under `examples/adapters/{hermes,openclaw,opencode,pi}` predate
this contract and are kept as legacy examples.
