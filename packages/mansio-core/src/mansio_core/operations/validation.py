"""Channel-name rules shared by every wire adapter.

The rules live in core because authorization and token-store prefix
registration already do; an adapter only maps a failure to a status.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from mansio_core.authz import DM_NAME_EXPECTED, DM_PREFIX, dm_channel_error
from mansio_core.operations import OperationError

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


def invalid_dm_name(channel: str) -> bool:
    """True if *channel* uses the ``dm:`` prefix but is not a valid DM name.

    Combines the structural rule shared with the authorization predicates
    (``mansio_core.authz``) with the character rule of the generic system channel
    pattern, so a name accepted here can always be published to later.
    """
    return channel.startswith(DM_PREFIX) and (
        dm_channel_error(channel) is not None or not _SYSTEM_CHANNEL_RE.match(channel)
    )


def validate_channel_name(channel: str, token_store: TokenStore | None = None) -> None:
    """Raise ``OperationError`` unless *channel* is a valid name.

    The message text is the one the HTTP API has always returned; the
    adapter owns the status mapping.
    """
    if invalid_dm_name(channel):
        raise OperationError("bad_request", DM_NAME_EXPECTED)

    is_reserved = any(channel.startswith(p) for p in _RESERVED_PREFIXES)

    if is_reserved:
        if not _SYSTEM_CHANNEL_RE.match(channel):
            raise OperationError("bad_request", "Invalid system channel name")
        return

    # Check for custom registered prefix (channel contains colon)
    if ":" in channel:
        prefix = channel.split(":")[0]
        if token_store and token_store.is_registered_prefix(prefix):
            if len(channel) < 3 or len(channel) > 64 or channel != channel.lower():
                raise OperationError("bad_request", "Invalid prefixed channel name")
            return
        raise OperationError(
            "bad_request",
            f"Prefix '{prefix}:' is not registered. Register custom prefixes via admin panel.",
        )

    # Plain user channel — no colons
    if len(channel) < 3 or not _USER_CHANNEL_RE.match(channel):
        raise OperationError(
            "bad_request",
            "Channel name must be 3-64 chars, start with letter, end with letter/digit, "
            "lowercase, no colons (use registered prefixes for namespaced channels)",
        )
    if not any(c.isalpha() for c in channel):
        raise OperationError("bad_request", "Channel name must contain at least one letter")
    if channel != channel.lower():
        raise OperationError("bad_request", "Channel name must be lowercase")
    if _NO_CONSECUTIVE_SPECIALS.search(channel):
        raise OperationError(
            "bad_request", "Channel name cannot contain consecutive special characters"
        )
