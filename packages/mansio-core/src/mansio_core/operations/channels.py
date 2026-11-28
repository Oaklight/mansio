"""Channel use cases.

Each operation takes an :class:`OperationContext`, a required
:class:`Principal`, and the path or DTO arguments. It applies the
authorization rules, calls the Bus, and returns the value the adapter
formats. ``NotImplementedError`` is left to the adapter, which owns the
501 bodies.
"""

from __future__ import annotations

from mansio_core.authz import DM_PREFIX
from mansio_core.contracts import ChannelCreateRequest, RegistryLookupRequest
from mansio_core.operations import OperationContext, OperationError
from mansio_core.operations.validation import validate_channel_name
from mansio_core.principal import Principal
from mansio_core.types import ChannelMeta

DetailRow = dict[str, object]


def list_channels(ctx: OperationContext, principal: Principal, detail: bool) -> list:
    """List channel names, or detail rows, filtered to what the caller may see."""
    user_id = principal.user_id

    if detail:
        rows: list[DetailRow] = ctx.bus.channels_detail()
        if user_id is None:
            return rows
        allowed = ctx.authz.can_list_channels(user_id, [row["name"] for row in rows])
        return [row for row in rows if allowed[row["name"]]]

    names = ctx.bus.channels(detail=False)
    if user_id is None:
        return names
    allowed = ctx.authz.can_list_channels(user_id, names)
    return [name for name in names if allowed[name]]


def create_channel(
    ctx: OperationContext, principal: Principal, dto: ChannelCreateRequest
) -> ChannelMeta:
    """Validate the name, resolve the owner, and create the channel."""
    if dto.name.startswith(DM_PREFIX):
        validate_channel_name(dto.name, token_store=ctx.token_store)

    owner = dto.owner or ""
    user_id = principal.user_id
    if user_id is not None:
        denial = ctx.authz.create_denial(user_id, dto.name)
        if denial:
            raise OperationError("forbidden", denial)
        owner = user_id  # scoped tokens own what they create
    elif not owner:
        raise OperationError("bad_request", "'owner' is required for supertoken/no-auth")

    try:
        return ctx.bus.create_channel(dto.name, owner, visibility=dto.visibility)
    except ValueError as exc:
        raise OperationError("conflict", str(exc)) from exc


def channel_meta(ctx: OperationContext, principal: Principal, channel: str) -> ChannelMeta:
    """Return channel metadata, or the same 404 a missing channel gets."""
    user_id = principal.user_id
    if user_id is not None and not ctx.authz.can_read(user_id, channel):
        # 404, not 403: a denied caller must not learn from the
        # status code that a private channel exists.  The message is
        # the same one an unknown channel returns.
        raise OperationError("not_found", f"Channel '{channel}' not found")

    meta = ctx.bus.get_channel_meta(channel)
    if meta is None:
        raise OperationError("not_found", f"Channel '{channel}' not found")
    return meta


def delete_channel(ctx: OperationContext, principal: Principal, channel: str) -> int:
    """Delete a channel and return the message count removed."""
    user_id = principal.user_id
    if user_id is not None:
        denial = ctx.authz.delete_channel_denial(user_id, channel)
        if denial:
            raise OperationError("forbidden", denial)

    count = ctx.bus.delete_channel(channel)
    if count == 0:
        raise OperationError("not_found", f"Channel '{channel}' not found or already empty")
    return count


def registry_lookup(
    ctx: OperationContext, principal: Principal, dto: RegistryLookupRequest
) -> bool:
    """Whether the token store knows *dto.user_id*."""
    store = ctx.token_store
    return store is not None and store.user_exists(dto.user_id)
