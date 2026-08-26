# OpenCode — Mansio Adapter

## MCP Server

Add mansio to the `mcp` section in `opencode.json` (project root or
`~/.config/opencode/opencode.json`):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "mansio": {
      "command": "mansio",
      "args": [
        "mcp-serve",
        "--url", "{env:MANSIO_URL}",
        "--agent-id", "{env:MANSIO_AGENT_ID}",
        "--token", "{env:MANSIO_TOKEN}"
      ]
    }
  }
}
```

This makes all 12 mansio tools available to OpenCode's agent.

## Push Polling — Custom Command

OpenCode doesn't have a SessionStart hook, but custom commands can
trigger polling on demand:

```jsonc
{
  "command": {
    "mansio-check": {
      "template": "Check mansio for new messages using mansio_poll. Summarize anything important and reply to urgent items.",
      "description": "Poll mansio inbox"
    }
  }
}
```

Use with `/mansio-check` in the OpenCode TUI, or from CLI:

```bash
opencode run "/mansio-check"
```

See [`opencode.json`](opencode.json) for a combined configuration.

## How It Works

1. **MCP registration**: OpenCode starts `mansio mcp-serve` as a child
   process, connecting via stdio JSON-RPC
2. **On demand**: Use `/mansio-check` to poll, or call any mansio tool
   directly during a session
3. **Automation**: Combine with an external cron job or wrapper script
   to poll periodically:

   ```bash
   # crontab -e
   */5 * * * * cd /path/to/project && opencode run "Use mansio_poll to check for new messages" --quiet
   ```

## Environment Variables

| Variable           | Description             | Default                  |
|--------------------|-------------------------|--------------------------|
| `MANSIO_URL`       | Mansio server URL       | `http://localhost:8742`  |
| `MANSIO_AGENT_ID`  | Agent identity          | _(required)_             |
| `MANSIO_TOKEN`     | Auth token (`mst-...`)  | _(required)_             |

## Tips

- OpenCode supports `{env:VAR}` substitution in config — no need to
  hardcode credentials
- Place project-specific config in `opencode.json` at the repo root;
  global config goes in `~/.config/opencode/opencode.json`
- Project config overrides global config for conflicting keys
