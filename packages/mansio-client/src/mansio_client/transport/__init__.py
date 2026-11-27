"""Client transports.

The HTTP/SSE implementation lives in :mod:`mansio_client.transport.http`;
this package re-exports it so ``from mansio_client.transport import
HttpTransport`` keeps working.  The interface itself is
:class:`mansio_core.transport.Transport`.

:func:`create_transport` picks an implementation by URL scheme.  A new
transport registers a factory with :func:`register_transport`; adding one
does not require a change to :class:`~mansio_client.client.MansioClient`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from mansio_client.transport.http import (
    SSE_JOIN_TIMEOUT,
    HttpTransport,
    MansioAPIError,
)
from mansio_client.transport.http import _Subscription as _Subscription

if TYPE_CHECKING:
    from mansio_core.transport import Transport


class TransportFactory(Protocol):
    """Build a transport for a URL.

    Implementations take the same arguments as :func:`create_transport`.
    """

    def __call__(self, url: str, /, *, user_id: str, token: str | None) -> Transport: ...


_TRANSPORT_FACTORIES: dict[str, TransportFactory] = {}


def register_transport(scheme: str, factory: TransportFactory) -> None:
    """Register *factory* for *scheme*.

    Schemes are case-insensitive.  Registering an existing scheme replaces
    the previous factory.

    Args:
        scheme: URL scheme without the trailing colon, e.g. ``"http"``.
        factory: Callable that builds a transport for that scheme.

    Raises:
        ValueError: If *scheme* is empty or contains ``":"``.
    """
    key = scheme.strip().lower()
    if not key:
        raise ValueError("transport scheme must not be empty")
    if ":" in key:
        raise ValueError(f"transport scheme {scheme!r} must not contain ':'")
    _TRANSPORT_FACTORIES[key] = factory


def create_transport(url: str, *, user_id: str, token: str | None = None) -> Transport:
    """Create a transport for *url* using its scheme.

    Args:
        url: Server URL, e.g. ``"https://mansio.example.com"``.
        user_id: User identifier passed to the transport.
        token: Optional Bearer token passed to the transport.

    Returns:
        The transport registered for the URL's scheme.

    Raises:
        ValueError: If the URL has no scheme or the scheme is not
            registered.  The message names the scheme and lists the
            supported ones.
    """
    scheme = urlsplit(url).scheme.lower()
    if not scheme:
        raise ValueError(f"cannot select a transport for {url!r}: URL has no scheme")
    try:
        factory = _TRANSPORT_FACTORIES[scheme]
    except KeyError:
        supported = ", ".join(sorted(_TRANSPORT_FACTORIES))
        raise ValueError(
            f"unsupported transport scheme {scheme!r} in {url!r}; supported schemes: {supported}"
        ) from None
    return factory(url, user_id=user_id, token=token)


register_transport("http", HttpTransport)
register_transport("https", HttpTransport)

__all__ = [
    "SSE_JOIN_TIMEOUT",
    "HttpTransport",
    "MansioAPIError",
    "TransportFactory",
    "create_transport",
    "register_transport",
]
