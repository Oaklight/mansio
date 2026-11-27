"""Opt-in coalesced delivery for :class:`MansioClient` subscriptions.

The coalescer batches messages that arrive inside one tumbling window and
delivers them in a single callback.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from mansio_core.types import Message

if TYPE_CHECKING:
    from mansio_client._state import ClientState
    from mansio_client.injectors import Injector

_log = logging.getLogger(__name__)

_TIMER_FACTORY = threading.Timer
"""Timer factory used by coalesced subscriptions; tests inject a fake."""


class _Coalescer:
    """Tumbling-window batch delivery for one subscription.

    The window starts with the first buffered message and flushes once, when
    the timer fires. Messages arriving after a flush start a new window.
    Own-message filtering happens in the transport before this class sees a
    message, so own messages never enter a batch.
    """

    def __init__(
        self,
        window_seconds: float,
        deliver: Callable[[list[Message]], None],
        on_error: Callable[[Message, Exception], None] | None,
        channel: str,
    ) -> None:
        self._window = window_seconds
        self._deliver = deliver
        self._on_error = on_error
        self._channel = channel
        self._lock = threading.Lock()
        self._batch: list[Message] = []
        self._timer: threading.Timer | None = None
        self._closed = False

    def add(self, msg: Message) -> None:
        """Buffer *msg*; start the window timer if this is the first one."""
        with self._lock:
            if self._closed:
                return
            self._batch.append(msg)
            if self._timer is None:
                timer = _TIMER_FACTORY(self._window, self.flush)
                timer.daemon = True
                self._timer = timer
                timer.start()

    def flush(self) -> None:
        """Deliver the buffered batch, if any."""
        with self._lock:
            batch = self._batch
            self._batch = []
            self._timer = None
        if not batch:
            return
        try:
            self._deliver(batch)
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(batch[-1], exc)
            else:
                _log.warning("coalesced callback failed for %s: %s", self._channel, exc)

    def close(self, *, flush: bool) -> None:
        """Stop the timer; deliver the pending batch only if *flush* is true."""
        with self._lock:
            self._closed = True
            timer = self._timer
            self._timer = None
            if timer is not None:
                timer.cancel()
        if flush:
            self.flush()
        else:
            with self._lock:
                self._batch.clear()


class CoalesceMixin:
    """Subscribe, unsubscribe, and listen with optional batch delivery."""

    def subscribe(
        self: ClientState,
        channel: str,
        callback: Callable[[Message], None] | Callable[[list[Message]], None],
        on_error: Callable[[Message, Exception], None] | None = None,
        *,
        include_own: bool = False,
        coalesce_ms: int = 0,
    ) -> str:
        """Subscribe to real-time notifications on a channel.

        Args:
            channel: Channel to subscribe to.
            callback: Called with each new Message. When *coalesce_ms* is
                positive it is called once per batch with a
                ``list[Message]`` instead.
            on_error: Called with ``(message, exception)`` when *callback*
                raises, so the subscriber can retry or record the failure.
                With coalescing, the message is the last one in the batch.
                When omitted, the exception is logged.
            include_own: Deliver messages this user sent (default False).
                Filtering happens before coalescing, so own messages never
                enter a batch.
            coalesce_ms: Opt-in batch window in milliseconds (default 0 =
                deliver each message immediately). Messages arriving in the
                same window are delivered in one callback.

        Returns:
            Subscription ID for unsubscribe().
        """
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must be >= 0")
        if coalesce_ms == 0:
            return self._transport.subscribe(
                channel,
                cast(Callable[[Message], None], callback),
                on_error=on_error,
                include_own=include_own,
            )
        coalescer = _Coalescer(
            coalesce_ms / 1000.0,
            cast(Callable[[list[Message]], None], callback),
            on_error,
            channel,
        )
        sub_id = self._transport.subscribe(
            channel, coalescer.add, on_error=on_error, include_own=include_own
        )
        self._coalescers[sub_id] = coalescer
        return sub_id

    def unsubscribe(self: ClientState, subscription_id: str) -> None:
        """Cancel a subscription, delivering any pending coalesced batch first."""
        coalescer = self._coalescers.pop(subscription_id, None)
        if coalescer is not None:
            coalescer.close(flush=True)
        self._transport.unsubscribe(subscription_id)

    def listen(
        self: ClientState,
        channels: list[str],
        injector: Injector,
        *,
        include_own: bool = False,
    ) -> list[str]:
        """Subscribe to multiple channels, routing messages through an injector.

        Convenience method that subscribes to each channel using the
        injector's :meth:`~Injector.inject` method as the SSE callback.
        Messages arriving on any subscribed channel are delivered to the
        injector in real time.

        Args:
            channels: List of channel names to subscribe to.
            injector: An :class:`~mansio_client.injectors.Injector`
                instance that receives each incoming message.
            include_own: Forward messages this user sent (default False).

        Returns:
            List of subscription IDs (one per channel). Pass these to
            :meth:`unsubscribe` to stop listening.

        The caller is responsible for calling ``injector.close()`` when
        done to release any resources held by the injector.

        Example::

            from mansio_client.injectors import ClaudeCodeInjector

            injector = ClaudeCodeInjector(project_dir=".")
            sub_ids = client.listen(["general", "inbox"], injector)
            # ... later ...
            for sid in sub_ids:
                client.unsubscribe(sid)
        """
        return [self.subscribe(ch, injector.inject, include_own=include_own) for ch in channels]
