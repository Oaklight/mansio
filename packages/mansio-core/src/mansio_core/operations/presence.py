"""Presence use cases.

A scoped caller may only report its own presence. The roster and the
single-agent lookup have no extra filter today; a missing agent is a 404.
"""

from __future__ import annotations

from mansio_core.contracts import PresenceHeartbeatRequest, PresenceListRequest
from mansio_core.operations import OperationContext, OperationError
from mansio_core.principal import Principal
from mansio_core.types import UserPresence


def touch(ctx: OperationContext, principal: Principal, dto: PresenceHeartbeatRequest) -> None:
    """Record a heartbeat for *dto.user_id*."""
    bound_user = principal.user_id
    if bound_user is not None and dto.user_id != bound_user:
        raise OperationError(
            "forbidden",
            f"Token bound to '{bound_user}', cannot heartbeat as '{dto.user_id}'",
        )
    ctx.bus.heartbeat(dto.user_id, dto.metadata)


def list_presence(
    ctx: OperationContext, principal: Principal, dto: PresenceListRequest
) -> list[UserPresence]:
    """Return every known agent with its computed status."""
    return ctx.bus.users(dto.timeout)


def get(
    ctx: OperationContext, principal: Principal, user_id: str, dto: PresenceListRequest
) -> UserPresence:
    """Return one agent's presence, or the 404 the API has always sent."""
    result = ctx.bus.user_status(user_id, dto.timeout)
    if result is None:
        raise OperationError("not_found", "agent not found")
    return result
