"""Typing surface shared by the :class:`MansioClient` mixins.

All state lives on :class:`~mansio_client.client.MansioClient`; the mixin
modules only declare what they use.  Everything here is defined under
``TYPE_CHECKING`` so the modules stay free of runtime coupling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal, overload

    from mansio_core.transport import Transport
    from mansio_core.types import Message

    from mansio_client.coalesce import _Coalescer
    from mansio_client.loop import LoopSuspected, _LoopGuard

    class ClientState(Protocol):
        """The attributes and methods the mixins rely on."""

        _user_id: str
        _display_name: str
        _transport: Transport
        _cursors: dict[str, str]
        _persisted: dict[str, str]
        _coalescers: dict[str, _Coalescer]
        _loop_guard: _LoopGuard | None
        _on_loop_suspected: Callable[[LoopSuspected], None] | None

        @property
        def _cursor_channel(self) -> str: ...

        @staticmethod
        def _parse_snapshot(payload: str) -> dict[str, str]: ...

        @staticmethod
        def _merge_cursors(a: dict[str, str], b: dict[str, str]) -> dict[str, str]: ...

        @staticmethod
        def _dm_channel(user_a: str, user_b: str) -> str: ...

        def _fetch_cursors(self) -> dict[str, str]: ...

        def channel_send(
            self,
            channel: str,
            content: str,
            msg_type: str = ...,
            metadata: dict | None = ...,
            parent_id: str | None = ...,
            intent: str | None = ...,
        ) -> str: ...

        def channel_read(
            self,
            channel: str,
            limit: int = ...,
            after: str | None = ...,
            order: Literal["oldest", "newest"] = ...,
            thread_id: str | None = ...,
            intent: str | None = ...,
            include_own: bool = ...,
        ) -> list[Message]: ...

        def channel_poll(self, channel: str, *, include_own: bool = ...) -> list[Message]: ...

        @overload
        def channel_list(self) -> list[str]: ...
        @overload
        def channel_list(self, *, detail: Literal[False]) -> list[str]: ...
        @overload
        def channel_list(self, *, detail: Literal[True]) -> list[dict]: ...

        def subscribe(
            self,
            channel: str,
            callback: Callable[[Message], None] | Callable[[list[Message]], None],
            on_error: Callable[[Message, Exception], None] | None = ...,
            *,
            include_own: bool = ...,
            coalesce_ms: int = ...,
        ) -> str: ...
