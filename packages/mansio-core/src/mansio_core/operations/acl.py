"""ACL use cases.

The manage gate, the grantor, and every not-found/NotImplemented outcome
match the HTTP API as it was before the move. ``NotImplementedError`` is
left to the adapter, which owns the 501 body.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mansio_core.contracts import ACLAddRequest, ACLSetRequest
from mansio_core.operations import OperationContext, OperationError
from mansio_core.principal import Principal
from mansio_core.types import ACLEntry


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def require_manage(ctx: OperationContext, principal: Principal, channel: str) -> None:
    """Raise ``forbidden`` unless the caller may manage *channel*.

    Privileged principals always pass.
    """
    user_id = principal.user_id
    if user_id is not None and not ctx.authz.can_manage_acl(user_id, channel):
        raise OperationError(
            "forbidden", f"Agent '{user_id}' lacks manage permission on '{channel}'"
        )


def get_acl(ctx: OperationContext, principal: Principal, channel: str) -> list[ACLEntry]:
    """Return the ACL entries of *channel*."""
    require_manage(ctx, principal, channel)
    return ctx.bus.get_acl(channel)


def set_acl(
    ctx: OperationContext, principal: Principal, channel: str, dto: ACLSetRequest
) -> list[ACLEntry]:
    """Replace the ACL of *channel* with *dto.entries*."""
    require_manage(ctx, principal, channel)

    now = _now_iso()
    granted_by = principal.user_id
    entries = [
        ACLEntry(
            channel=channel,
            user_id=spec.user_id,
            permission=spec.permission,
            granted_at=now,
            granted_by=granted_by,
        )
        for spec in dto.entries
    ]

    if ctx.bus.get_channel_meta(channel) is None:
        raise OperationError("not_found", f"Channel '{channel}' not found")
    ctx.bus.set_acl(channel, entries)
    return entries


def add_acl_entry(
    ctx: OperationContext, principal: Principal, channel: str, dto: ACLAddRequest
) -> ACLEntry:
    """Add one ACL entry to *channel*."""
    require_manage(ctx, principal, channel)

    entry = ACLEntry(
        channel=channel,
        user_id=dto.user_id,
        permission=dto.permission,
        granted_at=_now_iso(),
        granted_by=principal.user_id,
    )
    if ctx.bus.get_channel_meta(channel) is None:
        raise OperationError("not_found", f"Channel '{channel}' not found")
    ctx.bus.add_acl_entry(entry)
    return entry


def remove_acl_entry(
    ctx: OperationContext, principal: Principal, channel: str, user_id: str
) -> None:
    """Remove *user_id*'s ACL entry from *channel*."""
    require_manage(ctx, principal, channel)

    if not ctx.bus.remove_acl_entry(channel, user_id):
        raise OperationError("not_found", f"No ACL entry for '{user_id}' on '{channel}'")
