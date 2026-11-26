"""HTTP and IRC frontends, admin panel and server orchestration.

The core bus lives in :mod:`mansio_core`; this package owns everything that
speaks a network protocol or renders a page.
"""

from __future__ import annotations

__version__ = "0.3.0.dev1"

from mansio_web.frontends.base import Frontend
from mansio_web.frontends.http import HttpFrontend
from mansio_web.server import MansioServer
from mansio_web.transport_http import HttpTransport, MansioAPIError

__all__ = [
    "Frontend",
    "HttpFrontend",
    "HttpTransport",
    "MansioAPIError",
    "MansioServer",
]
