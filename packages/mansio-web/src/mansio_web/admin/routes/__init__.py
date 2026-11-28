"""Admin route modules.

Each module exposes ``register(app, ctx)`` and owns one URL group.  The
frozen :class:`RouteContext` carries everything the handlers need, so a
module never reaches back into ``AdminServer``.  ``AdminServer`` stays the
composition root and calls the registers in a fixed order.

Bus access goes through the operations in :mod:`mansio_core.operations`;
the error mapper below only turns an operation failure into the admin
error document.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mansio_core.contracts import ContractError, ErrorResult
from mansio_core.operations import OperationContext, OperationError

if TYPE_CHECKING:
    from mansio_core.token_store import TokenStore

    from ..auth import SessionAuth
    from ..view import AdminView

_OPERATION_STATUS = {
    "bad_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
}

_OPERATION_LABEL = {
    "bad_request": "Bad Request",
    "unauthorized": "Unauthorized",
    "forbidden": "Forbidden",
    "not_found": "Not Found",
    "conflict": "Conflict",
}


@dataclass(frozen=True, slots=True)
class RouteContext:
    """Dependencies shared by the admin route modules."""

    view: AdminView
    operations: OperationContext
    token_store: TokenStore | None
    auth: SessionAuth | None
    serve_ui: bool


def json_body(request: Any, message: str = "Request body must be valid JSON") -> Any:
    """Decode an admin request body, mapping decode failures to a contract error."""
    try:
        return request.json()
    except ValueError as exc:
        raise ContractError(message) from exc


def operation_error(err: OperationError) -> tuple[dict[str, str], int]:
    """Map an operation failure to the admin error document."""
    return (
        ErrorResult(error=_OPERATION_LABEL[err.kind], message=err.message).to_dict(),
        _OPERATION_STATUS[err.kind],
    )


__all__ = ["RouteContext", "json_body", "operation_error"]
