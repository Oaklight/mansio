"""Compatibility re-export of :mod:`mansio_core.types`.

The canonical domain types now live in ``mansio-core``.  This module keeps
``mansio_client.types`` importable for existing SDK users.
"""

from __future__ import annotations

from mansio_core.types import (
    ALLOWED_PERMISSIONS,
    PERMISSION_ALIASES,
    PERMISSION_LEVELS,
    REQUIRED_ALIASES,
    ROLE_PRESETS,
    USER_ID_PATTERN,
    USER_ID_RE,
    VERBS,
    ACLEntry,
    ChannelMeta,
    ClaimResult,
    Message,
    UserPresence,
    expand_permission,
    is_valid_user_id,
    normalize_required,
    permission_allows,
)

__all__ = [
    "ALLOWED_PERMISSIONS",
    "ACLEntry",
    "ChannelMeta",
    "ClaimResult",
    "Message",
    "PERMISSION_ALIASES",
    "PERMISSION_LEVELS",
    "REQUIRED_ALIASES",
    "ROLE_PRESETS",
    "USER_ID_PATTERN",
    "USER_ID_RE",
    "UserPresence",
    "VERBS",
    "expand_permission",
    "is_valid_user_id",
    "normalize_required",
    "permission_allows",
]
