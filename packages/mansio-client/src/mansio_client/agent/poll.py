"""The ``mansio-agent poll`` hook command."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from mansio_client.client import MansioClient
from mansio_client.env import env_or

from . import runtimes


def parse_channels(value: str | None) -> list[str] | None:
    """Parse ``--channels`` or ``MANSIO_CHANNELS`` into a list."""
    raw = value if value is not None else env_or("MANSIO_CHANNELS")
    if raw is None:
        return None
    channels = [part.strip() for part in raw.split(",") if part.strip()]
    return channels or None


def collect(client: Any, channels: list[str] | None) -> tuple[list[str], list[tuple[str, Any]]]:
    """Poll *channels*, or all non-system channels when *channels* is None."""
    if channels is None:
        listed = client.channel_list()
        channels = [
            channel
            for channel in listed
            if isinstance(channel, str) and not channel.startswith("_system:")
        ]

    found: list[tuple[str, Any]] = []
    for channel in channels:
        try:
            messages = client.channel_poll(channel)
        except Exception as exc:
            print(f"mansio-agent poll: skipping {channel}: {exc}", file=sys.stderr)
            continue
        found.extend((channel, message) for message in messages)
    return channels, found


def format_context(channels: list[str], messages: list[tuple[str, Any]]) -> str:
    """Render the hook context text."""
    if not channels:
        return "[mansio] No channels found."
    if not messages:
        return "[mansio] No new messages."

    lines = ["[mansio] Unread messages:", ""]
    for channel, message in messages:
        lines.append(f"--- #{channel} ---")
        lines.append(f"{message.sender}: {message.payload}")
    return "\n".join(lines)


def render(poll_format: str, context: str) -> str:
    """Render *context* in the runtime's hook output shape."""
    if poll_format in ("claude", "codex"):
        return json.dumps({"hookSpecificOutput": {"additionalContext": context}})
    return context


def run(
    *,
    poll_format: str,
    channels: list[str] | None,
    token: str | None,
    cwd: Path | None = None,
) -> int:
    """Run one poll. Returns a process exit code."""
    url, user_id = runtimes.resolve_connection(poll_format, cwd=cwd)
    if not url or not user_id:
        print(
            "mansio-agent poll: MANSIO_URL and MANSIO_USER_ID are required "
            "(environment or installed config)",
            file=sys.stderr,
        )
        return 1
    try:
        with MansioClient(url, user_id, token=token) as client:
            resolved, messages = collect(client, channels)
    except Exception as exc:
        print(f"mansio-agent poll: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(render(poll_format, format_context(resolved, messages)))
    return 0
