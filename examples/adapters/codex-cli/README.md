# Codex CLI Adapter

Integrates mansio with [Codex CLI](https://learn.chatgpt.com/docs) via
native MCP server support and lifecycle hooks.

## Setup

### 1. Configure the MCP Server

Add mansio as an MCP server in your Codex configuration.

**Per-project** (`.codex/config.toml`):

```toml
[mcp_servers.mansio]
command = "mansio"
args = [
  "mcp-serve",
  "--url", "http://localhost:8742",
  "--agent-id", "codex-agent",
  "--token", "mst-your-token-here"
]
```

**Per-user** (`~/.codex/config.toml`):

Same format — applies to all projects.

### 2. Add Polling Hooks

Poll mansio at session start to inject unread messages as context.

**`hooks.json` format** (`.codex/hooks.json` or `~/.codex/hooks.json`):

```json
{
  "description": "Mansio message polling for Codex sessions",
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "poll-mansio.sh",
            "timeout": 15,
            "statusMessage": "Checking mansio messages"
          }
        ]
      }
    ]
  }
}
```

**Inline TOML format** (`.codex/config.toml`):

```toml
[[hooks.SessionStart]]
matcher = "startup|resume"

[[hooks.SessionStart.hooks]]
type = "command"
command = "poll-mansio.sh"
timeout = 15
statusMessage = "Checking mansio messages"
```

### 3. Full Config Example

Combined `config.toml` with MCP server and polling hook:

```toml
[mcp_servers.mansio]
command = "mansio"
args = [
  "mcp-serve",
  "--url", "http://localhost:8742",
  "--agent-id", "codex-agent",
  "--token", "mst-your-token-here"
]

[[hooks.SessionStart]]
matcher = "startup|resume"

[[hooks.SessionStart.hooks]]
type = "mcp_tool"
server = "mansio"
tool = "mansio_poll"
timeout = 10
statusMessage = "Checking mansio inbox"

[hooks.SessionStart.hooks.input]
channel = "inbox"
```

## Environment Variables

```bash
export MANSIO_URL=http://localhost:8742
export MANSIO_AGENT_ID=codex-agent
export MANSIO_TOKEN=mst-your-token-here
```

## Differences from Claude Code

Codex CLI hooks are nearly identical to Claude Code hooks. Key differences:

- Config uses TOML (`config.toml`) in addition to JSON (`hooks.json`)
- Codex has a trust-review system — new hooks must be reviewed via
  `/hooks` before they run
- `prompt` and `agent` hook types are parsed but skipped in Codex
- MCP tool hooks use the same `mcp_tool` type and `${field}` templating
