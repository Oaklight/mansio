"""Admin route modules.

Each module exposes ``register(app, ctx)`` and owns one URL group.  The
frozen :class:`RouteContext` carries everything the handlers need, so a
module never reaches back into ``AdminServer``.  ``AdminServer`` stays the
composition root and calls the registers in a fixed order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mansio_core.bus import Bus
    from mansio_core.token_store import TokenStore

    from ..auth import SessionAuth
    from ..view import AdminView


@dataclass(frozen=True, slots=True)
class RouteContext:
    """Dependencies shared by the admin route modules."""

    view: AdminView
    bus: Bus
    token_store: TokenStore | None
    auth: SessionAuth | None
    serve_ui: bool


__all__ = ["RouteContext"]
