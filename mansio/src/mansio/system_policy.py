"""Default compaction policy for system channels."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mansio.protocols import Backend

CompactionPolicy = Callable[["Backend", str], None]


def system_channel_policy(backend: Backend, channel: str) -> None:
    """Auto-compact system channels to prevent unbounded growth.

    Called on every publish as an intentional hook point — the default
    policy early-returns for non-system channels with no overhead.

    - ``_system:registry`` — one registration per agent is sufficient.
    - ``_system:cursors:*`` — only the latest snapshot matters.

    Args:
        backend: The message backend (checked for Compactable support).
        channel: The channel that was just written to.
    """
    from mansio.protocols import Compactable

    if not isinstance(backend, Compactable):
        return
    if channel == "_system:registry":
        backend.compact(channel, keep_latest_per_sender=True)
    elif channel.startswith("_system:cursors:"):
        backend.compact(channel, max_messages=1)
