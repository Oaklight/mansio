"""Errors raised while parsing a wire contract."""

from __future__ import annotations


class ContractError(ValueError):
    """A request body or query parameter violates its contract.

    The message is safe to return to the caller.  Route handlers map this
    to HTTP 400 with the shared error envelope.

    Args:
        message: Human-readable, caller-safe explanation.
        field: Optional field or parameter name.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def as_error(self) -> dict[str, str]:
        """Return the shared error envelope for this failure."""
        return {"error": "Bad Request", "message": self.message}
