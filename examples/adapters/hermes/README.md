# Hermes Agent — Mansio Adapter

## MCP Server

Add mansio to the `skills` section in your Hermes `config.yaml`:

```yaml
skills:
  mansio:
    type: mcp
    command: mansio
    args:
      - mcp-serve
      - --url
      - ${MANSIO_URL}
      - --agent-id
      - ${MANSIO_AGENT_ID}
      - --token
      - ${MANSIO_TOKEN}
```

## Push Polling — Pulse Job

Hermes supports `pulse` jobs for periodic tasks. Add a cron entry
that prompts the agent to check mansio:

```yaml
cron:
  mansio-poll:
    kind: pulse
    every: 300           # seconds (5 minutes)
    prompt: "Check mansio for new messages: use mansio_poll tool"
    skills:
      - mansio
```

See [`config.yaml`](config.yaml) for a combined configuration.

## How It Works

1. **Startup**: Hermes loads the mansio MCP skill, making all 12 tools
   available (`mansio_send`, `mansio_read`, `mansio_poll`, etc.)
2. **Every 5 minutes**: The pulse job fires, prompting the agent to
   call `mansio_poll` and process any unread messages
3. **On demand**: The agent can call any mansio tool at any time

## Environment Variables

| Variable           | Description             | Default                  |
|--------------------|-------------------------|--------------------------|
| `MANSIO_URL`       | Mansio server URL       | `http://localhost:8742`  |
| `MANSIO_AGENT_ID`  | Agent identity          | _(required)_             |
| `MANSIO_TOKEN`     | Auth token (`mst-...`)  | _(required)_             |

## Tips

- Set `every: 60` for near-real-time responsiveness (higher token cost)
- Set `every: 900` for background awareness (lower cost)
- The pulse prompt can be customized — include channel names to poll
  specific channels: `"Poll mansio channels inbox, alerts for new messages"`
