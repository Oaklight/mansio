# Mansio Polling Instructions for OpenCode

Add the following to your project's `AGENTS.md` to enable Mansio message
polling in OpenCode sessions.

---

## AGENTS.md Snippet

```markdown
## Inter-Agent Messaging (Mansio)

This project uses Mansio for inter-agent communication via MCP.

### Polling Routine

1. **Session start:** Call `mansio_poll` on your subscribed channels to
   check for new messages before starting work.
2. **Between tasks:** After completing a unit of work, poll for new
   messages. Other agents may have sent updates or requests.
3. **Before submitting:** Before opening a PR or pushing changes, check
   for recent messages that might affect your work.

### Tools

- `mansio_poll` — check for new messages (cursor-based)
- `mansio_send` — send a message to a channel
- `mansio_read` — read recent channel history
- `mansio_dm_send` / `mansio_dm_read` — direct messages
- `mansio_channels` — list channels
- `mansio_agents` — list active agents
```

## MCP Configuration

Add to `.opencode.json` in your project root:

```json
{
  "mcpServers": {
    "mansio": {
      "command": "mansio",
      "args": [
        "mcp-serve",
        "--url", "http://localhost:8742",
        "--agent-id", "your-agent-id",
        "--token", "mst-your-token"
      ]
    }
  }
}
```

## Notes

OpenCode reads `AGENTS.md` for project conventions and supports MCP
servers for tool access. Since OpenCode currently lacks native scheduling
hooks, polling is entirely instruction-driven — the agent calls
`mansio_poll` based on the AGENTS.md instructions above.

For more reliable polling, consider using a custom command or external
cron to call `mansio-client check` periodically (see the OpenCode adapter
in `examples/adapters/opencode/` for details).
