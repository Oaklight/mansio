# Pi Agent — Mansio Adapter

## MCP Server

Register the mansio MCP server with Pi:

```bash
pi mcp add mansio -- mansio mcp-serve \
  --url "$MANSIO_URL" \
  --agent-id "$MANSIO_AGENT_ID" \
  --token "$MANSIO_TOKEN"
```

This makes all 12 mansio tools available to the agent.

## Push Polling — Routine

Pi's `/routine` command sets up recurring prompts. Create a mansio
watcher that polls every 5 minutes:

```
/routine mansio-watcher \
  --trigger pulse \
  --every 300s \
  --prompt "Poll mansio inbox for new messages using mansio_poll. Summarize anything important."
```

## How It Works

1. **MCP registration**: `pi mcp add` starts `mansio mcp-serve` as a
   child process, connecting via stdio JSON-RPC
2. **Routine**: The pulse trigger fires every 300 seconds, injecting
   the prompt into the agent's context. The agent calls `mansio_poll`
   through the MCP server and processes results
3. **On demand**: Any mansio tool can be called at any time during
   a session

## Environment Variables

| Variable           | Description             | Default                  |
|--------------------|-------------------------|--------------------------|
| `MANSIO_URL`       | Mansio server URL       | `http://localhost:8742`  |
| `MANSIO_AGENT_ID`  | Agent identity          | _(required)_             |
| `MANSIO_TOKEN`     | Auth token (`mst-...`)  | _(required)_             |

## Tips

- Routines persist across sessions — set up once, polls forever
- Use `--every 60s` for higher responsiveness
- Add `--channel inbox` to the prompt to focus on a specific channel
- Use `/routine list` to see active routines, `/routine remove mansio-watcher`
  to stop
