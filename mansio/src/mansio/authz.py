"""Channel authorization policy shared by frontends.

These rules are structural: they follow from the channel name and the
metadata stored by a ``ChannelStore`` backend, not from HTTP details.
A frontend authenticates the caller first; this module decides whether the
authenticated ``user_id`` may perform a verb on a channel.  Supertokens and
no-auth mode bypass these checks at the frontend, because they have no
``user_id`` to evaluate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mansio.types import permission_allows

if TYPE_CHECKING:
    from mansio.bus import Bus
    from mansio.types import Message

PRIVATE_CHANNEL_PREFIXES = ("notebook:", "memory:")
DM_PREFIX = "dm:"


def dm_participants(channel: str) -> tuple[str, str] | None:
    """Return the two participants of a well-formed DM channel.

    A DM channel name is exactly ``dm:<a>:<b>``: three colon-separated
    segments with a non-empty user id in each participant slot.  Any other
    name that starts with ``dm:`` is malformed and returns ``None``.

    This is the single parser for DM names.  Callers that must reject a
    malformed name on input use :func:`dm_channel_error`; authorization
    predicates use this function and treat ``None`` as "no participants".
    """
    if not channel.startswith(DM_PREFIX):
        return None
    parts = channel.split(":")
    if len(parts) != 3:
        return None
    _, first, second = parts
    if not first or not second:
        return None
    return first, second


def dm_channel_error(channel: str) -> str | None:
    """Return a client error message for a malformed ``dm:`` channel name.

    Returns ``None`` for well-formed DM names and for names that do not use
    the ``dm:`` prefix at all.
    """
    if channel.startswith(DM_PREFIX) and dm_participants(channel) is None:
        return "Invalid dm channel name; expected 'dm:<user_a>:<user_b>'"
    return None


def is_private_channel(channel: str) -> bool:
    """Return True if *channel* is a DM or private-prefix channel.

    Private channels are ``dm:*``, ``notebook:*``, and ``memory:*``.
    Everything else (plain channels, ``_system:``, ``broadcast:``, and
    registered prefixes) is public.

    Any name starting with ``dm:`` is private, including a malformed one.
    Failing closed keeps pre-existing malformed DM rows readable only by
    their sender, even though new malformed names are rejected on input.
    """
    if channel.startswith(DM_PREFIX):
        return True
    return any(channel.startswith(p) for p in PRIVATE_CHANNEL_PREFIXES)


def is_channel_member(user_id: str, channel: str) -> bool:
    """Check if *user_id* is an owner/participant of a private channel.

    Ownership is determined by structural parsing, not substring matching,
    to prevent false positives (e.g. user ``notebook`` must not match
    ``notebook:alice``).

    - ``notebook:<owner>`` / ``memory:<owner>`` → the owner segment
    - ``dm:<A>:<B>`` → exactly A and B
    """
    if channel.startswith(DM_PREFIX):
        participants = dm_participants(channel)
        return participants is not None and user_id in participants

    for prefix in PRIVATE_CHANNEL_PREFIXES:
        if channel.startswith(prefix):
            return user_id == channel[len(prefix) :]

    return False


def user_involved(user_id: str, channel: str, sender: str) -> bool:
    """Check if an agent may see a message (public channel, sender, member)."""
    if not is_private_channel(channel):
        return True
    if sender == user_id:
        return True
    return is_channel_member(user_id, channel)


def broadcast_write_allowed(bus: Bus | None, channel: str, user_id: str) -> bool:
    """Return True if *user_id* may write to broadcast channel *channel*.

    The channel owner (its first publisher) and agents holding an entry
    that grants ``publish`` may post.  A channel that does not exist yet is
    created for the first publisher by ``Bus.ensure_channel()``, whose
    create step is atomic, so two concurrent first publishers cannot both
    win ownership.  Backends without ``ChannelStore`` keep the old
    supertoken-only rule.
    """
    if bus is None:
        return False
    try:
        meta = bus.ensure_channel(channel, owner=user_id)
        acl = bus.get_acl(channel)
    except NotImplementedError:
        return False
    if meta.owner == user_id:
        return True
    return any(
        entry.user_id == user_id and permission_allows(entry.permission, "publish") for entry in acl
    )


class ChannelAuthorizer:
    """Structural authorization policy for one Bus."""

    def __init__(self, bus: Bus) -> None:
        self._bus = bus

    # ── publish ──────────────────────────────────────────────

    def publish_denial(self, user_id: str, channel: str, msg_type: str = "") -> str | None:
        """Return a denial message if *user_id* may not publish, else None."""
        if channel.startswith("_system:"):
            own_cursors = f"_system:cursors:{user_id}"
            if channel == "_system:agents" and msg_type == "presence":
                return None
            if channel == own_cursors:
                return None
            return f"Channel '{channel}' is reserved for system use"

        if channel.startswith("broadcast:"):
            if broadcast_write_allowed(self._bus, channel, user_id):
                return None
            return (
                f"Channel '{channel}' is reserved for the channel owner "
                "and agents granted write access"
            )

        if is_private_channel(channel) and not is_channel_member(user_id, channel):
            if channel.startswith(PRIVATE_CHANNEL_PREFIXES):
                owner = channel.split(":", 1)[1]
                return f"Channel '{channel}' belongs to agent '{owner}'"
            return f"Channel '{channel}' is private"

        return None

    def can_publish(self, user_id: str, channel: str, msg_type: str = "") -> bool:
        """True if *user_id* may publish *msg_type* to *channel*."""
        return self.publish_denial(user_id, channel, msg_type) is None

    # ── read ─────────────────────────────────────────────────

    def can_read(self, user_id: str, channel: str) -> bool:
        """True if *user_id* may read *channel* (public or a member)."""
        return not is_private_channel(channel) or is_channel_member(user_id, channel)

    def can_view_message(self, user_id: str, channel: str, sender: str) -> bool:
        """True if *user_id* may see a message in *channel* from *sender*."""
        return user_involved(user_id, channel, sender)

    def can_list_channel(self, user_id: str, channel: str) -> bool:
        """True if *channel* appears in *user_id*'s channel listing."""
        return self.can_read(user_id, channel)

    # ── delete ───────────────────────────────────────────────

    def delete_channel_denial(self, user_id: str, channel: str) -> str | None:
        """Return a denial message if *user_id* may not delete, else None."""
        if channel.startswith("_system:"):
            return (
                f"Channel '{channel}' is a system channel; "
                "only admin tokens can delete system channels"
            )
        if is_private_channel(channel):
            if not is_channel_member(user_id, channel):
                return f"Token for '{user_id}' cannot delete channel '{channel}'"
            return None
        return "Scoped tokens cannot delete shared channels"

    def can_delete_channel(self, user_id: str, channel: str) -> bool:
        """True if *user_id* may delete *channel*."""
        return self.delete_channel_denial(user_id, channel) is None

    def can_delete_message(self, user_id: str, message: Message | None) -> bool:
        """True if *user_id* may delete *message* (sender only, as before)."""
        return message is not None and message.sender == user_id

    # ── ACL management ───────────────────────────────────────

    def can_manage_acl(self, user_id: str, channel: str) -> bool:
        """True if *user_id* holds the ``manage`` verb on *channel*."""
        try:
            return self._bus.check_access(channel, user_id, "manage")
        except NotImplementedError:
            return False
