"""Use-case layer contracts.

Operations sit between a wire adapter and the Bus: they take a
:class:`~mansio_core.principal.Principal`, apply the channel and ACL rules,
call the Bus, and return a DTO. Adapters only map wire input in and
:class:`OperationError` out.

Nothing calls these types yet; they are the contract for the message
operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from mansio_core.authz import ChannelAuthorizer
    from mansio_core.bus import Bus
    from mansio_core.token_store import TokenStore

OperationErrorKind = Literal["bad_request", "unauthorized", "forbidden", "not_found", "conflict"]

OPERATION_ERROR_KINDS: tuple[OperationErrorKind, ...] = (
    "bad_request",
    "unauthorized",
    "forbidden",
    "not_found",
    "conflict",
)


class OperationError(Exception):
    """A use-case failure that an adapter maps to a wire status.

    Attributes:
        kind: The failure class; the adapter owns the status mapping.
        message: Human-readable detail for the wire body.
    """

    def __init__(self, kind: OperationErrorKind, message: str) -> None:
        if kind not in OPERATION_ERROR_KINDS:
            raise ValueError(f"unknown operation error kind {kind!r}")
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Dependencies an operation needs, assembled once per adapter.

    Attributes:
        bus: The attached Bus.
        authz: The channel authorization policy.
        token_store: The token store, or ``None`` when the adapter has none.
        allow_unauthenticated: Whether the composition root started this
            adapter with ``--no-auth``. Only then may a ``NO_AUTH``
            principal act without channel restrictions.
    """

    bus: Bus
    authz: ChannelAuthorizer
    token_store: TokenStore | None
    allow_unauthenticated: bool
