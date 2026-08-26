"""Message injection layer for agent frameworks.

Provides a protocol and concrete implementations for injecting mansio
messages into various agent framework contexts. Each injector adapts
the delivery mechanism to suit the target framework.

Usage::

    from mansio_client import MansioClient
    from mansio_client.injectors import ClaudeCodeInjector

    client = MansioClient(url, agent_id, token=token)
    injector = ClaudeCodeInjector(project_dir=".")
    sub_ids = client.listen(["general", "inbox"], injector)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mansio_client.types import Message


def _msg_to_dict(msg: Message) -> dict[str, Any]:
    """Serialize a Message to a plain dict for JSON output."""
    d: dict[str, Any] = {
        "id": msg.id,
        "channel": msg.channel,
        "sender": msg.sender,
        "msg_type": msg.msg_type,
        "payload": msg.payload,
        "timestamp": msg.timestamp,
    }
    if msg.metadata is not None:
        d["metadata"] = msg.metadata
    if msg.parent_id is not None:
        d["parent_id"] = msg.parent_id
    if msg.thread_id is not None:
        d["thread_id"] = msg.thread_id
    return d


# ── Protocol ──────────────────────────────────────────────────────


@runtime_checkable
class Injector(Protocol):
    """Delivers a mansio Message into an agent framework's context.

    Implementers handle one concern: take a Message and make the agent
    "see" it through whatever mechanism the framework supports.
    """

    def inject(self, message: Message) -> None:
        """Inject a single message into the agent's context.

        Args:
            message: The mansio Message to deliver.
        """
        ...

    def close(self) -> None:
        """Release resources (flush files, close connections).

        Called when the client shuts down. Implementations that hold no
        resources may leave this as a no-op.
        """
        ...


# ── MailboxInjector ───────────────────────────────────────────────


class MailboxInjector:
    """Append messages as JSONL to a mailbox file.

    Universal fallback injector — any framework can consume JSONL via
    a cron job, polling script, or MCP tool.

    Each line is a self-contained JSON object with all message fields.
    Thread-safe: uses a lock around file writes.

    Args:
        path: Path to the JSONL mailbox file.
        max_lines: Optional rotation threshold. When the file exceeds
            this many lines, it is truncated to keep only the most
            recent ``max_lines // 2`` entries. Set to 0 to disable.

    Example::

        injector = MailboxInjector("/tmp/mansio-inbox.jsonl")
        client.listen(["general"], injector)
    """

    def __init__(self, path: str, *, max_lines: int = 0) -> None:
        self._path = os.path.expanduser(path)
        self._max_lines = max_lines
        self._lock = threading.Lock()
        self._line_count = 0

        # Count existing lines if the file already exists
        if os.path.isfile(self._path):
            with open(self._path) as f:
                self._line_count = sum(1 for _ in f)

    @property
    def path(self) -> str:
        """Path to the JSONL mailbox file."""
        return self._path

    def inject(self, message: Message) -> None:
        """Append message as a JSON line to the mailbox file."""
        line = json.dumps(_msg_to_dict(message), ensure_ascii=False)

        with self._lock:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._line_count += 1

            if self._max_lines > 0 and self._line_count > self._max_lines:
                self._rotate()

    def _rotate(self) -> None:
        """Keep only the most recent half of max_lines entries."""
        keep = self._max_lines // 2
        with open(self._path, encoding="utf-8") as f:
            lines = f.readlines()
        trimmed = lines[-keep:]
        with open(self._path, "w", encoding="utf-8") as f:
            f.writelines(trimmed)
        self._line_count = len(trimmed)

    def close(self) -> None:
        """No-op — file handles are opened/closed per write."""


# ── ClaudeCodeInjector ────────────────────────────────────────────


class ClaudeCodeInjector(MailboxInjector):
    """Inject messages for Claude Code via a project-local JSONL mailbox.

    Extends :class:`MailboxInjector` with Claude Code conventions:

    - Default path: ``<project_dir>/.claude/mansio-inbox.jsonl``
    - Messages are stored in the same format that ``poll-mansio.sh``
      produces, so Claude Code hooks can consume them directly.

    The mailbox is read by:

    1. A ``SessionStart`` hook calling ``poll-mansio.sh`` (see
       ``examples/adapters/claude-code/``), or
    2. An MCP tool call to ``mansio_poll`` via the MCP server.

    Args:
        project_dir: Root of the Claude Code project. Defaults to the
            current working directory.
        max_lines: Rotation threshold (default 500).

    Example::

        injector = ClaudeCodeInjector(project_dir="/home/user/myproject")
        client.listen(["general"], injector)
    """

    def __init__(self, project_dir: str = ".", *, max_lines: int = 500) -> None:
        path = os.path.join(
            os.path.expanduser(project_dir), ".claude", "mansio-inbox.jsonl"
        )
        super().__init__(path, max_lines=max_lines)


# ── OpenClawInjector ──────────────────────────────────────────────


class OpenClawInjector:
    """Write Markdown message files to an OpenClaw workspace directory.

    OpenClaw agents read new files from their workspace during heartbeat
    ticks. Each injected message becomes a separate ``.md`` file named
    ``mansio_{timestamp}_{sender}_{channel}.md``.

    Args:
        directory: Target directory for message files. Defaults to
            ``workspace/memory/mansio/`` (relative to cwd).

    Example::

        injector = OpenClawInjector("/home/agent/.openclaw/workspace/memory/mansio")
        client.listen(["general"], injector)
    """

    def __init__(self, directory: str = "workspace/memory/mansio") -> None:
        self._directory = os.path.expanduser(directory)
        self._lock = threading.Lock()

    @property
    def directory(self) -> str:
        """Target directory for message files."""
        return self._directory

    def inject(self, message: Message) -> None:
        """Write the message as a Markdown file in the workspace."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        safe_sender = _safe_filename(message.sender)
        safe_channel = _safe_filename(message.channel)
        filename = f"mansio_{ts}_{safe_sender}_{safe_channel}.md"

        content = self._format_markdown(message)

        with self._lock:
            os.makedirs(self._directory, exist_ok=True)
            filepath = os.path.join(self._directory, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

    @staticmethod
    def _format_markdown(message: Message) -> str:
        """Format a message as readable Markdown."""
        lines = [
            f"# Mansio message from {message.sender}",
            "",
            f"- **Channel:** {message.channel}",
            f"- **Type:** {message.msg_type}",
            f"- **Time:** {message.timestamp}",
            f"- **ID:** {message.id}",
        ]
        if message.parent_id:
            lines.append(f"- **Reply to:** {message.parent_id}")
        lines.extend(["", "---", "", message.payload, ""])
        return "\n".join(lines)

    def close(self) -> None:
        """No-op — file handles are opened/closed per write."""


# ── WebhookInjector ───────────────────────────────────────────────


class WebhookInjector:
    """POST messages to a webhook URL.

    Handles the "Interrupt" communication plane — notifying external
    services (Telegram bots, Slack webhooks, custom endpoints) when
    an agent receives a mansio message.

    Uses the stdlib ``urllib.request`` module (no external dependencies).
    Failures are silently ignored to avoid blocking the SSE subscription
    loop.

    Args:
        url: Webhook endpoint URL.
        headers: Optional extra HTTP headers (e.g. auth tokens).
        timeout: HTTP request timeout in seconds.

    Example::

        injector = WebhookInjector(
            "https://hooks.slack.com/services/T.../B.../xxx",
            headers={"Content-Type": "application/json"},
        )
        client.listen(["alerts"], injector)
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout

    @property
    def url(self) -> str:
        """Webhook endpoint URL."""
        return self._url

    def inject(self, message: Message) -> None:
        """POST the message as JSON to the webhook URL."""
        import urllib.request

        payload = json.dumps(_msg_to_dict(message), ensure_ascii=False).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        headers.update(self._headers)

        req = urllib.request.Request(
            self._url,
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout):
                pass
        except Exception:
            # Best-effort: don't block the subscription loop on webhook failures
            pass

    def close(self) -> None:
        """No-op — each request is independent."""


# ── Helpers ───────────────────────────────────────────────────────


def _safe_filename(s: str) -> str:
    """Replace characters that are unsafe in filenames."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
