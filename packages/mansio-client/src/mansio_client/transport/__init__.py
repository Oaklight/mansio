"""Client transports.

The HTTP/SSE implementation lives in :mod:`mansio_client.transport.http`;
this package re-exports it so ``from mansio_client.transport import
HttpTransport`` keeps working.  The interface itself is
:class:`mansio_core.transport.Transport`.
"""

from __future__ import annotations

from mansio_client.transport.http import (
    SSE_JOIN_TIMEOUT,
    HttpTransport,
    MansioAPIError,
)
from mansio_client.transport.http import _Subscription as _Subscription

__all__ = [
    "SSE_JOIN_TIMEOUT",
    "HttpTransport",
    "MansioAPIError",
]
