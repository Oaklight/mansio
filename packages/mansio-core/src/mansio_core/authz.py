"""Channel authorization policy shared by adapters.

These rules are structural: they follow from the channel name and the
metadata stored by a ``ChannelStore`` backend, not from HTTP details.
An adapter authenticates the caller first; this module decides whether the
authenticated ``user_id`` may perform a verb on a channel.  Supertokens and
no-auth mode bypass these checks at the adapter, because they have no
``user_id`` to evaluate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from mansio_core.types import is_valid_user_id, normalize_required, permission_allows

if TYPE_CHECKING:
    from mansio_core.bus import Bus
    from mansio_core.types import Message

PRIVATE_CHANNEL_PREFIXES = ("notebook:", "memory:")
DM_PREFIX = "dm:"
DM_NAME_EXPECTED = "Invalid dm channel name; expected 'dm:<user_a>:<user_b>' with valid user ids"


def dm_participants(channel: str) -> tuple[str, str] | None:
    """Return the two participants of a well-formed DM channel.

    A DM channel name is exactly ``dm:<a>:<b>``: three colon-separated
    segments with a valid agent id in each participant slot.  Any other
    name that starts with ``dm:`` is malformed and returns ``None``.

    The participant slots use the same rule as token creation
    (:func:`mansio_core.types.is_valid_user_id`): 3-64 characters, lowercase
    alphanumerics and hyphens, starting and ending with an alphanumeric.

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
    if not is_valid_user_id(first) or not is_valid_user_id(second):
        return None
    return first, second


def dm_channel_error(channel: str) -> str | None:
    """Return a client error message for a malformed ``dm:`` channel name.

    Returns ``None`` for well-formed DM names and for names that do not use
    the ``dm:`` prefix at all.
    """
    if channel.startswith(DM_PREFIX) and dm_participants(channel) is None:
        return DM_NAME_EXPECTED
    return None


def is_private_channel(channel: str) -> bool:
    """Return True if *channel* is a DM or private-prefix channel.

    Private channels are ``dm:*``, ``notebook:*``, and ``memory:*``.
    Everything else (plain channels, ``_system:``, ``broadcast:``, and
    registered prefixes) is public.

    Any name starting with ``dm:`` is private, including a malformed one.
    The input paths reject malformed names, but a row that already exists
    (or a caller that does not go through those paths) must not become
    public: no scoped token is a member, so only the sender is involved.
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


def access_allowed(
    owner: str | None,
    visibility: str | None,
    permission: str | None,
    user_id: str,
    required: str = "read",
) -> bool:
    """Decide a stored-channel access question.

    This is the single copy of the channel-store rule: backends fetch the
    rows, this function decides.  ``owner is None`` means the channel has no
    metadata row, which every backend treats as public.

    Args:
        owner: Stored channel owner, or ``None`` for an unregistered channel.
        visibility: Stored visibility (``"public"`` or ``"private"``), or
            ``None`` when the channel is unregistered.
        permission: The caller's stored permission preset/verb on the
            channel, or ``None`` when there is no ACL entry.
        user_id: Agent whose access is being decided.
        required: Verb needed (legacy ``write``/``admin`` are mapped to
            ``publish``/``manage`` by :func:`permission_allows`).

    Returns:
        True if access is granted.
    """
    if owner is None:
        return True
    if owner == user_id:
        return True
    if visibility == "public" and normalize_required(required) in ("read", "publish"):
        return True
    return permission is not None and permission_allows(permission, required)


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

    def _check_access(self, user_id: str, channel: str, verb: str) -> bool:
        """Ask the channel store whether *user_id* holds *verb* on *channel*.

        Backends without ``ChannelStore`` have no owner, visibility, or ACL
        rows, so every channel is public there.  ``Bus.check_access`` already
        answers True for them; the ``NotImplementedError`` guard keeps that
        behavior if a store-backed method surfaces the same error.
        """
        try:
            return self._bus.check_access(channel, user_id, verb)
        except NotImplementedError:
            return True

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

        if is_private_channel(channel):
            if is_channel_member(user_id, channel):
                return None
            if channel.startswith(PRIVATE_CHANNEL_PREFIXES):
                owner = channel.split(":", 1)[1]
                return f"Channel '{channel}' belongs to agent '{owner}'"
            return f"Channel '{channel}' is private"

        # Plain and registered-prefix channels: owner, visibility, and ACL
        # live in the channel store, so the structural rules alone cannot
        # decide.  This is where visibility="private" is enforced.
        if not self._check_access(user_id, channel, "publish"):
            return f"Token for '{user_id}' cannot publish to channel '{channel}'"

        return None

    def can_publish(self, user_id: str, channel: str, msg_type: str = "") -> bool:
        """True if *user_id* may publish *msg_type* to *channel*."""
        return self.publish_denial(user_id, channel, msg_type) is None

    # ── create ───────────────────────────────────────────────

    def create_denial(self, user_id: str, channel: str) -> str | None:
        """Return a denial message if *user_id* may not create *channel*'s row.

        Applies the structural rules of the private namespaces, the same way
        :meth:`publish_denial` does: an agent may create a ``notebook:`` or
        ``memory:`` namespace only for itself, and a ``dm:`` name only when it
        is one of the two participants.  ``_system:`` names are reserved for
        supertokens and no-auth callers, which never reach this method.

        A ``broadcast:`` topic is reserved for its first publisher: a scoped
        token claims ownership by publishing, which auto-creates the row, so
        creating one directly is denied.  A supertoken or no-auth caller may
        pre-create a topic and set its owner explicitly.

        Plain channels stay open to every scoped token.
        """
        if channel.startswith("_system:"):
            return f"Channel '{channel}' is reserved for system use"

        if channel.startswith("broadcast:"):
            return f"Channel '{channel}' is reserved for its first publisher"

        if is_private_channel(channel) and not is_channel_member(user_id, channel):
            if channel.startswith(PRIVATE_CHANNEL_PREFIXES):
                owner = channel.split(":", 1)[1]
                return f"Channel '{channel}' belongs to agent '{owner}'"
            return f"Channel '{channel}' is private"

        return None

    def can_create_channel(self, user_id: str, channel: str) -> bool:
        """True if *user_id* may create *channel*'s metadata row."""
        return self.create_denial(user_id, channel) is None

    # ── claim ────────────────────────────────────────────────

    def claim_denial(self, user_id: str, channel: str) -> str | None:
        """Return a denial message if *user_id* may not claim from *channel*.

        Claiming takes work out of a queue, so the publish rule applies.
        One exception: a ``broadcast:`` channel that does not exist yet is
        denied instead of being auto-created.  ``publish_denial`` creates it
        for the first publisher (``broadcast_write_allowed``), and an
        attempted claim must not make the caller the channel owner.
        """
        if channel.startswith("broadcast:"):
            try:
                exists = self._bus.get_channel_meta(channel) is not None
            except NotImplementedError:
                exists = False
            if not exists:
                return (
                    f"Channel '{channel}' is reserved for the channel owner "
                    "and agents granted write access"
                )
        return self.publish_denial(user_id, channel)

    # ── read ─────────────────────────────────────────────────

    def can_read(self, user_id: str, channel: str) -> bool:
        """True if *user_id* may read *channel* (public or a member).

        Private-prefix and ``dm:`` channels are decided by name.  Plain and
        registered-prefix channels consult the channel store, which enforces
        ``visibility`` and ACL entries.
        """
        if is_private_channel(channel):
            return is_channel_member(user_id, channel)
        return self._check_access(user_id, channel, "read")

    def can_view_message(self, user_id: str, channel: str, sender: str) -> bool:
        """True if *user_id* may see a message in *channel* from *sender*."""
        return user_involved(user_id, channel, sender)

    def can_list_channel(self, user_id: str, channel: str) -> bool:
        """True if *channel* appears in *user_id*'s channel listing."""
        return self.can_list_channels(user_id, [channel])[channel]

    def can_list_channels(self, user_id: str, channels: Sequence[str]) -> dict[str, bool]:
        """Batch form of :meth:`can_list_channel`.

        Private-prefix and ``dm:`` names are decided by name, exactly as the
        single-channel form does.  Every other name is resolved in one
        ``Bus.check_access_many`` call, so listing a channel set costs a fixed
        number of backend round trips instead of one per channel.

        Args:
            user_id: Agent whose listing is being filtered.
            channels: Channel names to decide.

        Returns:
            Mapping of channel name to the answer
            :meth:`can_list_channel` would give for that name.
        """
        result: dict[str, bool] = {}
        public: list[str] = []
        for channel in channels:
            if is_private_channel(channel):
                result[channel] = is_channel_member(user_id, channel)
            else:
                public.append(channel)
        if public:
            try:
                result.update(self._bus.check_access_many(public, user_id, "read"))
            except NotImplementedError:
                result.update({channel: True for channel in public})
        return result

    # ── delete ───────────────────────────────────────────────

    def delete_channel_denial(self, user_id: str, channel: str) -> str | None:
        """Return a denial message if *user_id* may not delete, else None.

        Name-private channels keep their membership rule and ``_system:*``
        stays closed.  Plain and registered-prefix channels need a stored
        row and the ``delete`` verb on it: that is the owner, or an ACL entry
        granting ``delete`` (the ``owner`` preset includes it; ``manage``
        alone controls the ACL, not deletion).  An unregistered channel has
        no owner and stays under the shared-channel denial.  ``broadcast:*``
        keeps its own rule and is not part of this path.
        """
        if channel.startswith("_system:"):
            return (
                f"Channel '{channel}' is a system channel; "
                "only admin tokens can delete system channels"
            )
        if is_private_channel(channel):
            if not is_channel_member(user_id, channel):
                return f"Token for '{user_id}' cannot delete channel '{channel}'"
            return None
        if channel.startswith("broadcast:"):
            return "Scoped tokens cannot delete shared channels"
        try:
            registered = self._bus.get_channel_meta(channel) is not None
        except NotImplementedError:
            registered = False
        if registered and self._check_access(user_id, channel, "delete"):
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
