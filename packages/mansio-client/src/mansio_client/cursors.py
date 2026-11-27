"""Persisted poll cursors for :class:`MansioClient`.

Cursors live on a per-user ``_system:cursors:<user>`` channel so a poll
position survives a client restart.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mansio_client._state import ClientState

_log = logging.getLogger(__name__)


class CursorMixin:
    """Cursor restore/save and unread counting."""

    @property
    def _cursor_channel(self: ClientState) -> str:
        return f"_system:cursors:{self._user_id}"

    @staticmethod
    def _parse_snapshot(payload: str) -> dict[str, str]:
        """Decode a stored snapshot, ignoring entries of the wrong shape."""
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise TypeError(f"cursor snapshot is {type(data).__name__}, not a mapping")
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def _fetch_cursors(self: ClientState) -> dict[str, str]:
        """Read the cursor snapshot currently stored on the server.

        The server keeps only the newest snapshot for a user, so a
        single newest-first message is all there is to read.
        """
        msgs = self._transport.query(self._cursor_channel, limit=1, order="newest")
        for msg in msgs:
            if msg.msg_type != "cursor_snapshot":
                continue
            try:
                return self._parse_snapshot(msg.payload)
            except (json.JSONDecodeError, TypeError) as exc:
                # An unreadable snapshot is not recoverable, but refusing to
                # run is worse than restarting from an empty position, so
                # report it and carry on with no stored cursors.
                _log.warning(
                    "ignoring unreadable cursor snapshot for %s: %s",
                    self._user_id,
                    exc,
                )
        return {}

    def _restore_cursors(self: ClientState) -> None:
        """Load stored cursors at startup.

        Transport failures propagate: a client that starts with no
        position silently replays every message it has already seen,
        which is worse than failing to start.
        """
        self._cursors = self._fetch_cursors()
        self._persisted = dict(self._cursors)

    @staticmethod
    def _merge_cursors(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
        """Combine two cursor maps, keeping the later position per channel.

        Message IDs are time-ordered (UUIDv7), so the larger string is
        the more recent message.
        """
        merged = dict(a)
        for channel, cursor in b.items():
            current = merged.get(channel)
            if current is None or cursor > current:
                merged[channel] = cursor
        return merged

    def _save_cursors(self: ClientState) -> None:
        """Write advanced cursors to the server, if any have advanced.

        Reads the stored snapshot and merges rather than overwriting it,
        so a session that polls one channel cannot roll back the
        position another session recorded for a different channel.
        """
        if self._cursors == self._persisted:
            return
        try:
            remote = self._fetch_cursors()
            merged = self._merge_cursors(remote, self._cursors)
            if merged != remote:
                self._transport.publish(
                    self._cursor_channel,
                    self._user_id,
                    "cursor_snapshot",
                    json.dumps(merged),
                )
            # Adopt the merged view: another session sharing this user ID
            # may have advanced further than we have, and the user ID is
            # one logical reader.
            self._cursors = merged
            self._persisted = dict(merged)
        except Exception:
            # Leave _persisted untouched so the unsaved advance is
            # retried by the next poll or by close().
            _log.warning("failed to save poll cursors for %s", self._user_id, exc_info=True)

    def check_unread(self: ClientState, *, include_own: bool = False) -> dict[str, int]:
        """Return per-channel unread message counts without advancing cursors.

        Queries each non-system channel for messages newer than the
        last-polled cursor.  Cursors are **not** advanced, so a
        subsequent :meth:`channel_poll` still returns the same messages.

        Args:
            include_own: Count messages this user sent (default False,
                matching :meth:`channel_poll`).

        Returns:
            Dict mapping channel name to unread count (only channels
            with at least one unread message are included).
        """
        channels = self.channel_list()
        user_channels = [ch for ch in channels if not ch.startswith("_system:")]
        unread: dict[str, int] = {}
        for ch in user_channels:
            cursor = self._cursors.get(ch)
            msgs = self._transport.query(ch, after=cursor)
            if not include_own:
                msgs = [m for m in msgs if m.sender != self._user_id]
            if msgs:
                unread[ch] = len(msgs)
        return unread
