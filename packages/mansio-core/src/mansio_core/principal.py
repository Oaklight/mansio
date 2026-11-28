"""Authenticated identity for one request.

A ``Principal`` says who is calling, in a form the use-case layer can read
without knowing which wire protocol the request arrived on. It replaces
the three-state ``auth_result`` (``str | None | True``) that made callers
reconstruct identity from Python types.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mansio_core.types import is_valid_user_id


class PrincipalKind(str, Enum):
    """Closed set of identities a wire adapter can produce.

    ``SERVICE`` is reserved for non-user service callers; nothing produces
    it yet.
    """

    USER = "user"
    SUPERTOKEN = "supertoken"
    ADMIN = "admin"
    SERVICE = "service"
    NO_AUTH = "no_auth"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller.

    Invariant: ``kind is PrincipalKind.USER`` if and only if ``user_id``
    holds a valid id. Every other kind carries no id.

    Provenance is kept on purpose: ``NO_AUTH`` is not the same caller as a
    supertoken. It acts without channel restrictions only when the
    composition root set ``OperationContext.allow_unauthenticated``.

    Attributes:
        kind: Identity kind.
        user_id: The scoped user id; ``None`` for every privileged kind.

    Raises:
        ValueError: If the kind and ``user_id`` disagree, or the id is
            empty or invalid.
    """

    kind: PrincipalKind
    user_id: str | None

    def __post_init__(self) -> None:
        if self.kind is PrincipalKind.USER:
            if not self.user_id:
                raise ValueError("a user principal requires a non-empty user_id")
            if not is_valid_user_id(self.user_id):
                raise ValueError(f"invalid user id {self.user_id!r}")
        elif self.user_id is not None:
            raise ValueError(f"a {self.kind.value} principal carries no user_id")

    @classmethod
    def user(cls, user_id: str) -> Principal:
        """Return a scoped user principal."""
        return cls(kind=PrincipalKind.USER, user_id=user_id)

    @classmethod
    def supertoken(cls) -> Principal:
        """Return a supertoken principal."""
        return cls(kind=PrincipalKind.SUPERTOKEN, user_id=None)

    @classmethod
    def admin(cls) -> Principal:
        """Return the principal for a live admin session."""
        return cls(kind=PrincipalKind.ADMIN, user_id=None)

    @classmethod
    def service(cls) -> Principal:
        """Return a service principal (reserved; no wire adapter yet)."""
        return cls(kind=PrincipalKind.SERVICE, user_id=None)

    @classmethod
    def no_auth(cls) -> Principal:
        """Return the principal for an API served with ``--no-auth``."""
        return cls(kind=PrincipalKind.NO_AUTH, user_id=None)

    @property
    def is_privileged(self) -> bool:
        """Whether the caller is not bound to one user id.

        This says what the identity is, not what it may do: ``NO_AUTH`` is
        privileged only where the operation context allows unauthenticated
        access.
        """
        return self.kind is not PrincipalKind.USER
