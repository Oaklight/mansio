"""Response and channel-validation helpers shared by route modules.

Only helpers used by more than one route group live here; module-specific
helpers stay with their group.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from mansio_core.authz import (
    DM_NAME_EXPECTED,
    DM_PREFIX,
    ChannelAuthorizer,
    dm_channel_error,
)
from mansio_core.contracts import ContractError, ErrorResult

if TYPE_CHECKING:
    from mansio_core.token_store import TokenStore


# Channel naming rules:
# - User channels: 3-64 chars, must contain a letter, start with [a-z],
#   end with [a-z0-9], no consecutive special chars
# - System/auto channels (_system:, dm:, notebook:, memory:, broadcast:)
#   are exempt from user rules — validated separately
# User channels: no colons (reserved for system prefixes), Unicode letters OK
# End anchor [^\W_] = letters/digits (no trailing underscore)
_USER_CHANNEL_RE = re.compile(r"^(?=[^\W\d_])[\w.-]{1,63}[^\W_]\Z")
_NO_CONSECUTIVE_SPECIALS = re.compile(r"[._-]{2}")
_RESERVED_PREFIXES = ("_system:", "dm:", "notebook:", "memory:", "broadcast:")
_SYSTEM_CHANNEL_RE = re.compile(r"^[\w_][\w:.-]{1,126}[\w]\Z")


def _invalid_dm_name(channel: str) -> bool:
    """True if *channel* uses the ``dm:`` prefix but is not a valid DM name.

    Combines the structural rule shared with the authorization predicates
    (``mansio_core.authz``) with the character rule of the generic system channel
    pattern, so a name accepted here can always be published to later.
    """
    return channel.startswith(DM_PREFIX) and (
        dm_channel_error(channel) is not None or not _SYSTEM_CHANNEL_RE.match(channel)
    )


def _validate_channel_name(
    channel: str, token_store: TokenStore | None = None
) -> tuple[dict, int] | None:
    """Validate channel name. Returns error tuple or None if OK."""
    if _invalid_dm_name(channel):
        return _error("Bad Request", DM_NAME_EXPECTED, 400)

    is_reserved = any(channel.startswith(p) for p in _RESERVED_PREFIXES)

    if is_reserved:
        if not _SYSTEM_CHANNEL_RE.match(channel):
            return _error("Bad Request", "Invalid system channel name", 400)
        return None

    # Check for custom registered prefix (channel contains colon)
    if ":" in channel:
        prefix = channel.split(":")[0]
        if token_store and token_store.is_registered_prefix(prefix):
            if len(channel) < 3 or len(channel) > 64 or channel != channel.lower():
                return _error("Bad Request", "Invalid prefixed channel name", 400)
            return None
        return _error(
            "Bad Request",
            f"Prefix '{prefix}:' is not registered. Register custom prefixes via admin panel.",
            400,
        )

    # Plain user channel — no colons
    if len(channel) < 3 or not _USER_CHANNEL_RE.match(channel):
        return _error(
            "Bad Request",
            "Channel name must be 3-64 chars, start with letter, end with letter/digit, "
            "lowercase, no colons (use registered prefixes for namespaced channels)",
            400,
        )
    if not any(c.isalpha() for c in channel):
        return _error("Bad Request", "Channel name must contain at least one letter", 400)
    if channel != channel.lower():
        return _error("Bad Request", "Channel name must be lowercase", 400)
    if _NO_CONSECUTIVE_SPECIALS.search(channel):
        return _error(
            "Bad Request", "Channel name cannot contain consecutive special characters", 400
        )

    return None


def _validate_and_auth_publish(
    auth_result: Any,
    sender: str,
    channel: str,
    msg_type: str = "",
    token_store: TokenStore | None = None,
    authz: ChannelAuthorizer | None = None,
) -> tuple[dict, int] | None:
    """Validate channel name and enforce publish auth.

    Returns error tuple or None if OK.
    """
    err = _validate_channel_name(channel, token_store=token_store)
    if err:
        return err

    if not isinstance(auth_result, str):
        return None

    # Sender identity: token must match the declared sender
    if sender != auth_result:
        return _error(
            "Forbidden", f"Token bound to '{auth_result}', cannot publish as '{sender}'", 403
        )

    denial = authz.publish_denial(auth_result, channel, msg_type) if authz else None
    if denial:
        return _error("Forbidden", denial, 403)

    return None


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
