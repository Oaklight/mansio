"""HTTP and IRC adapters, admin panel and server orchestration.

The core bus lives in :mod:`mansio_core`; this package owns everything that
speaks a network protocol or renders a page.  The client transport lives in
:mod:`mansio_client`, not here.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.3.0.dev1"

from mansio_web.adapters.base import ServerAdapter
from mansio_web.adapters.http import HttpAdapter
from mansio_web.server import MansioServer

__all__ = [
    "ServerAdapter",
    "HttpAdapter",
    "MansioServer",
]

_MOVED_TO_CLIENT = {
    "HttpTransport": "from mansio_client.transport import HttpTransport",
    "MansioAPIError": "from mansio_client.transport import MansioAPIError",
}


def __getattr__(name: str) -> Any:
    """Point removed transport names at their new package."""
    moved = _MOVED_TO_CLIENT.get(name)
    if moved is not None:
        raise ImportError(
            f"mansio_web.{name} moved to the mansio-client package; install "
            f"mansio-client and use `{moved}`"
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
