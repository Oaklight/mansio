#!/usr/bin/env bash
# poll-mansio.sh — Poll mansio for unread messages and output as JSON context.
#
# Used by framework hooks (Claude Code, Codex CLI, etc.) to inject
# unread messages into the agent's context at session start.
#
# Environment variables:
#   MANSIO_URL       — Server URL (required, or pass --server)
#   MANSIO_AGENT_ID  — Agent ID  (required, or pass --agent)
#   MANSIO_TOKEN     — API token (optional, or pass --api-token)
#   MANSIO_CHANNELS  — Comma-separated channels to poll (default: all)
#   MANSIO_LIMIT     — Max messages per channel (default: 20)
#
# Output: JSON on stdout with additionalContext for the agent.
# Exit 0: success (context injected). Exit 1: error.

set -euo pipefail

CHANNELS="${MANSIO_CHANNELS:-}"
LIMIT="${MANSIO_LIMIT:-20}"

# Build base command
CMD=(mansio-client)
[ -n "${MANSIO_URL:-}" ]      && CMD+=(--server "$MANSIO_URL")
[ -n "${MANSIO_AGENT_ID:-}" ] && CMD+=(--agent "$MANSIO_AGENT_ID")
[ -n "${MANSIO_TOKEN:-}" ]    && CMD+=(--api-token "$MANSIO_TOKEN")

# If no channels specified, discover them
if [ -z "$CHANNELS" ]; then
    CHANNELS=$("${CMD[@]}" channels 2>/dev/null | tr '\n' ',' | sed 's/,$//')
fi

if [ -z "$CHANNELS" ]; then
    # No channels found — output empty context
    echo '{"hookSpecificOutput":{"additionalContext":"[mansio] No channels found."}}'
    exit 0
fi

# Poll each channel and collect messages
CONTEXT="[mansio] Unread messages:\n"
HAS_MESSAGES=false

IFS=',' read -ra CHAN_ARRAY <<< "$CHANNELS"
for ch in "${CHAN_ARRAY[@]}"; do
    ch=$(echo "$ch" | xargs)  # trim whitespace
    [ -z "$ch" ] && continue
    # Skip system channels
    [[ "$ch" == _system:* ]] && continue

    MSGS=$("${CMD[@]}" poll -c "$ch" -n "$LIMIT" 2>/dev/null || true)
    if [ -n "$MSGS" ]; then
        HAS_MESSAGES=true
        CONTEXT+="\\n--- #${ch} ---\\n"
        while IFS= read -r line; do
            SENDER=$(echo "$line" | python3 -c "import sys,json; m=json.load(sys.stdin); print(m.get('sender','?'))" 2>/dev/null || echo "?")
            PAYLOAD=$(echo "$line" | python3 -c "import sys,json; m=json.load(sys.stdin); print(m.get('payload',''))" 2>/dev/null || echo "")
            CONTEXT+="${SENDER}: ${PAYLOAD}\\n"
        done <<< "$MSGS"
    fi
done

if [ "$HAS_MESSAGES" = false ]; then
    echo '{"hookSpecificOutput":{"additionalContext":"[mansio] No new messages."}}'
else
    # Escape for JSON
    ESCAPED=$(printf '%s' "$CONTEXT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null)
    echo "{\"hookSpecificOutput\":{\"additionalContext\":${ESCAPED}}}"
fi
