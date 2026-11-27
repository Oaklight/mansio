"""HTTP frontend route modules.

Each module exposes ``register(app, ctx)`` and owns one contiguous URL
group.  The frozen :class:`RouteContext` carries everything the handlers
need, so no route reaches back into ``HttpFrontend``.  ``HttpFrontend``
stays the composition root and calls the registers in a fixed order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from mansio_core.authz import ChannelAuthorizer
    from mansio_core.bus import Bus
    from mansio_core.token_store import TokenStore


@dataclass(frozen=True, slots=True)
class RouteContext:
    """Dependencies shared by the HTTP route modules."""

    bus: Bus
    authz: ChannelAuthorizer
    token_store: TokenStore | None
    allow_unauthenticated: bool
    max_query_limit: int
    cors_origin: str
    note_activity: Callable[[str], None]


__all__ = ["RouteContext"]
