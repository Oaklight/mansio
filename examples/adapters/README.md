# Mansio Push Adapters

MCP is pull-only — agents call tools, the server cannot push messages.
Each agent framework has its own mechanism for periodic checks.
These adapters bridge the gap by polling mansio at session start and/or
on a recurring schedule, then surfacing new messages as agent context.

## How It Works

```
┌──────────────┐         ┌─────────────┐         ┌──────────────┐
│ Agent        │  stdio  │ mansio      │  HTTP   │ mansio       │
│ Framework    │◄───────►│ mcp-serve   │◄───────►│ server       │
│ (MCP client) │         │ (JSON-RPC)  │         │ (:8742)      │
└──────────────┘         └─────────────┘         └──────────────┘
       │                                                │
       │  SessionStart hook ─► mansio-client poll ──────┘
       │  (injects unread messages as context)
```

Every adapter provides two integration points:

1. **MCP server** — gives the agent direct access to all 12 mansio
   tools (`mansio_send`, `mansio_read`, `mansio_poll`, etc.)
2. **Polling hook** — runs at session start or on a timer to inject
   unread messages into the agent's context window

## Adapter Matrix

| Framework   | MCP Server | Hook System      | Polling Method        | Priority |
|-------------|------------|------------------|-----------------------|----------|
| Claude Code | Native     | `settings.json`  | SessionStart hook     | P1       |
| Codex CLI   | Native     | `hooks.json`     | SessionStart hook     | P1       |
| Hermes      | Native     | `config.yaml`    | Cron/pulse job        | P2       |
| OpenClaw    | Via skill  | Cron system      | Cron agentTurn        | P2       |
| Pi          | Native     | `/routine`       | Routine command       | P2       |
| OpenCode    | Native     | Config           | Startup hook          | P3       |

## Prerequisites

Install `mansio-client` (for CLI polling) and/or `mansio` (for MCP server):

```bash
pip install mansio-client          # CLI: mansio-client poll, check, send
pip install mansio                 # Server + MCP: mansio mcp-serve
```

Set connection defaults via environment variables:

```bash
export MANSIO_URL=http://localhost:8742
export MANSIO_AGENT_ID=my-agent
export MANSIO_TOKEN=mst-your-token-here
```

## Choosing an Adapter

- **Claude Code / Codex CLI** — if your agent runs in one of these,
  use the native MCP + hooks integration. Zero extra dependencies.
- **OpenClaw** — use the cron adapter for background polling alongside
  the mansio skill for tool access.
- **Hermes** — use the cron/pulse adapter with the mansio skill.
- **Other frameworks** — adapt the shell polling script
  (`poll-mansio.sh`) to your framework's hook or scheduler system.

## Files

Each adapter directory contains:

- `README.md` — setup instructions specific to that framework
- Configuration files (hooks, settings, cron configs)
- `poll-mansio.sh` — reusable polling script (where applicable)
