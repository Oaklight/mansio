"""Queue use cases.

The scoped caller is always the claimer; a privileged caller must name one
in the body. Status and ack hide a message the caller may not read behind
the same answer a message without state gets.
"""

from __future__ import annotations

from mansio_core.contracts import QueueAckRequest, QueueClaimRequest, QueueStatusRequest
from mansio_core.operations import OperationContext, OperationError
from mansio_core.principal import Principal
from mansio_core.types import ClaimResult


def _claimed_by(principal: Principal, from_body: str | None) -> str:
    """Resolve the claimer: the scoped caller, or an explicit body value."""
    user_id = principal.user_id
    if user_id is not None:
        return user_id
    if not from_body:
        raise OperationError("bad_request", "Missing 'claimed_by'")
    return from_body


def claim(
    ctx: OperationContext, principal: Principal, dto: QueueClaimRequest
) -> ClaimResult | None:
    """Claim the oldest unclaimed or lease-expired message."""
    claimed_by = _claimed_by(principal, dto.claimed_by)

    user_id = principal.user_id
    if user_id is not None:
        denial = ctx.authz.claim_denial(user_id, dto.channel)
        if denial:
            raise OperationError("forbidden", denial)

    return ctx.bus.queue_claim(dto.channel, claimed_by, lease_seconds=dto.lease_seconds)


def ack(ctx: OperationContext, principal: Principal, dto: QueueAckRequest) -> ClaimResult | None:
    """Acknowledge a claimed message; ``None`` means not acknowledged."""
    claimed_by = _claimed_by(principal, dto.claimed_by)

    user_id = principal.user_id
    if user_id is not None:
        message = ctx.bus.get_message(dto.message_id)
        if message is not None and not ctx.authz.can_read(user_id, message.channel):
            # Same answer as "not claimed by you": an unauthorized caller
            # learns nothing about the message's existence.
            return None

    return ctx.bus.queue_ack(dto.message_id, claimed_by)


def status(ctx: OperationContext, principal: Principal, dto: QueueStatusRequest) -> dict | None:
    """Queue state of one message; ``None`` hides denial and absence alike."""
    user_id = principal.user_id
    if user_id is not None:
        message = ctx.bus.get_message(dto.message_id)
        if message is not None and not ctx.authz.can_read(user_id, message.channel):
            # Same body as a message without queue state, so the answer does
            # not confirm that a private message exists.
            return None

    return ctx.bus.queue_status(dto.message_id)
