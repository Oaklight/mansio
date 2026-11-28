"""Delivery semantics for agent-facing surfaces.

:class:`AgentDelivery` is the single home for how messages reach an agent:
pull (cursor, at-most-once), queue (lease, at-least-once), and stream
(push, advisory).  Formatting stays with the caller — the MCP tool result
and the ``mansio-agent poll`` hook text are presentation layers.
"""

from mansio_client.delivery.agent import (
    DEFAULT_WAIT_SECONDS,
    MAX_WAIT_SECONDS,
    WAIT_POLL_INTERVAL_SECONDS,
    AgentDelivery,
    WaitResult,
)

__all__ = [
    "AgentDelivery",
    "DEFAULT_WAIT_SECONDS",
    "MAX_WAIT_SECONDS",
    "WAIT_POLL_INTERVAL_SECONDS",
    "WaitResult",
]
