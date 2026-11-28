"""Response and channel-validation helpers shared by route modules.

Only helpers used by more than one route group live here; module-specific
helpers stay with their group. Channel-name rules live in
:mod:`mansio_core.operations.validation`; the wrapper below only turns an
operation error into the HTTP error document.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mansio_core.contracts import ContractError, ErrorResult
from mansio_core.operations import OperationError
from mansio_core.operations.validation import validate_channel_name

if TYPE_CHECKING:
    from mansio_core.token_store import TokenStore

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


def _validate_channel_name(
    channel: str, token_store: TokenStore | None = None
) -> tuple[dict, int] | None:
    """Validate channel name. Returns error tuple or None if OK."""
    try:
        validate_channel_name(channel, token_store=token_store)
    except OperationError as err:
        return _operation_error(err)
    return None


def _operation_error(err: OperationError) -> tuple[dict, int]:
    """Map an operation failure to its HTTP error document."""
    return _error(_OPERATION_LABEL[err.kind], err.message, _OPERATION_STATUS[err.kind])


def _error_body(error: str, message: str) -> dict[str, str]:
    """Return one of the shared error documents."""
    return ErrorResult(error=error, message=message).to_dict()


def _error(error: str, message: str, status: int) -> tuple[dict[str, str], int]:
    """Return an error document with its HTTP status."""
    return _error_body(error, message), status


def _bad_request(err: ContractError) -> tuple[dict[str, str], int]:
    """Return a contract error as an HTTP 400 response."""
    return _error_body("Bad Request", err.message), 400


def _json_body(request: Any, message: str = "Request body must be valid JSON") -> Any:
    """Decode the request body, mapping JSON failures to a contract error."""
    try:
        return request.json()
    except Exception as exc:
        raise ContractError(message) from exc
