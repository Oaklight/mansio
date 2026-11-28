"""Server-sent events routes and per-connection session helpers."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from mansio_core.contracts import (
    ContractError,
    StreamEvent,
    WarningEvent,
    parse_stream_event,
    stream_event_to_json,
    warning_event_to_json,
)
from mansio_core.operations import OperationContext, OperationError, streams
from mansio_core.operations.streams import StreamRequest
from mansio_core.principal import Principal

from mansio_web._vendor.httpserver import App, StreamingResponse

from ._common import _error, _operation_error

if TYPE_CHECKING:
    from collections.abc import Callable

    from mansio_core.types import Message

    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


@dataclass
class _SSESession:
    """Bundles per-connection SSE state to reduce parameter sprawl."""

    queue: asyncio.Queue[str | None]
    sub_ids: list[str]
    operations: OperationContext
    channels: list[str]
    principal: Principal
    last_event_id: str | None = None
    drop_lock: threading.Lock | None = None
    drop_counter: list[int] | None = None
    # Opt-in: drop messages whose sender is the authenticated caller, for
    # live delivery and Last-Event-ID replay alike.  No effect when the
    # connection has no bound user (supertoken, no-auth).
    exclude_own: bool = False
    # Called as the stream stays alive so an idle subscriber still counts
    # as present. None when the connection has no user identity.
    on_activity: Callable[[], None] | None = None


async def _setup_sse_subscriptions(
    operations: OperationContext,
    ch_list: list[str],
    principal: Principal,
    exclude_own: bool = False,
) -> tuple[asyncio.Queue[str | None], list[str], threading.Lock, list[int]]:
    """Subscribe to bus channels and return (queue, subscription_ids, drop_lock, drop_counter).

    Bus callbacks are sync and may run on any thread (the publisher's
    thread). We use ``loop.call_soon_threadsafe`` to safely enqueue
    data into the asyncio.Queue from non-event-loop threads.

    Returns a ``[dropped_count]`` list (mutable single-element) and
    a lock protecting it, so the async generator can report and reset
    the counter.

    The subscription itself runs in a worker thread: the read check can
    reach the channel store.
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)
    drop_lock = threading.Lock()
    drop_counter: list[int] = [0]  # mutable single-element for cross-thread access

    def _safe_enqueue(data: str) -> None:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            # Backpressure: drop oldest, enqueue new event
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(data)
            with drop_lock:
                drop_counter[0] += 1

    def _on_message(target_ch: str, message: Message) -> None:
        data = stream_event_to_json(StreamEvent(channel=target_ch, message=message))
        loop.call_soon_threadsafe(_safe_enqueue, data)

    sub_ids = await asyncio.to_thread(
        streams.subscribe,
        operations,
        principal,
        StreamRequest(channels=list(ch_list), exclude_own=exclude_own),
        _on_message,
    )

    return q, sub_ids, drop_lock, drop_counter


# Max messages to replay per channel on reconnect.  For a client
# subscribed to N channels the total replayed events can be up to
# _SSE_REPLAY_LIMIT × N.
_SSE_REPLAY_LIMIT = 500


def _format_sse_event(data: str, event_id: str) -> str:
    """Format a single SSE event with optional id: and data: fields.

    If *event_id* is falsy the ``id:`` line is omitted entirely to
    avoid resetting the client's ``lastEventId`` to the empty string
    (which would break cursor-based resume on reconnect).
    """
    if event_id:
        return f"id: {event_id}\ndata: {data}\n\n"
    return f"data: {data}\n\n"


def _format_sse_warning(event: WarningEvent) -> str:
    """Format one typed warning event (``event: warning`` + JSON ``data:``)."""
    return f"event: warning\ndata: {warning_event_to_json(event)}\n\n"


def _drain_drop_counter(
    drop_lock: threading.Lock | None,
    drop_counter: list[int] | None,
) -> str:
    """Return a typed warning frame if events were dropped, else empty string.

    ``dropped`` is connection-wide: the queue is shared by every channel of
    the connection, so the reader cannot attribute the loss to one channel
    and should re-query all subscribed channels. The warning is emitted
    after the next live event or on the next keepalive tick, whichever
    comes first, so a stream that goes quiet after drops still reports
    them within the keepalive interval. It is best-effort: a stream that
    closes before then may never send it.
    """
    if drop_lock is None or drop_counter is None:
        return ""
    with drop_lock:
        dropped = drop_counter[0]
        drop_counter[0] = 0
    if dropped:
        return _format_sse_warning(WarningEvent(kind="dropped", count=dropped))
    return ""


async def _replay_missed_events(session: _SSESession) -> Any:
    """Yield SSE events for messages published after ``last_event_id``.

    When a channel's replay fills ``_SSE_REPLAY_LIMIT``, a typed
    ``truncated`` warning for that channel precedes its events.  The query
    cannot tell whether more messages exist beyond the limit, so the
    client should re-query; the warning can be a false positive when
    exactly the limit is waiting. Unlike ``dropped``, this warning names
    one channel.
    """
    replayed = await asyncio.to_thread(
        streams.replay,
        session.operations,
        session.principal,
        StreamRequest(channels=session.channels, exclude_own=session.exclude_own),
        session.last_event_id or "",
        _SSE_REPLAY_LIMIT,
    )
    for channel, missed, truncated in replayed:
        if truncated:
            yield _format_sse_warning(
                WarningEvent(kind="truncated", count=_SSE_REPLAY_LIMIT, channel=channel)
            )
        for message in missed:
            yield _format_sse_event(
                stream_event_to_json(StreamEvent(channel=channel, message=message)), message.id
            )


async def _sse_event_generator(session: _SSESession) -> Any:
    """Async generator that yields SSE events from the queue.

    If *last_event_id* is set (via ``Last-Event-ID`` header), replays
    missed messages from the bus before switching to live streaming.
    Each event includes an ``id:`` field (the message ID) so the
    W3C SSE client can resume on reconnect.

    When events are dropped due to slow consumption, a typed warning
    event is emitted after the next event or keepalive tick so the client
    knows to re-query every subscribed channel.  A replay that fills
    ``_SSE_REPLAY_LIMIT`` is flagged as ``truncated`` for that channel.
    ``: connected`` and ``: keepalive`` remain comments.
    """
    try:
        yield ": connected\n\n"

        # ── Replay missed messages on reconnect ──────────────────
        if session.last_event_id:
            async for event in _replay_missed_events(session):
                yield event

        # NOTE: Subscriptions are active during replay, so a message
        # published in the replay window can appear in both the replay
        # results and the live queue (duplicate).  SSE clients that track
        # ``lastEventId`` deduplicate naturally; for others the window
        # is small and payloads are idempotent.

        # ── Live stream ──────────────────────────────────────────
        while True:
            # An open subscription is evidence the subscriber is present,
            # even while it sends no requests of its own.
            if session.on_activity is not None:
                session.on_activity()
            try:
                data = await asyncio.wait_for(session.queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                # Report drops even when the stream is otherwise quiet.
                warning = _drain_drop_counter(session.drop_lock, session.drop_counter)
                if warning:
                    yield warning
                continue
            if data is None:
                break
            # The SSE id: line carries the message id.
            try:
                event_id = parse_stream_event(data).message.id
            except ContractError:
                event_id = ""
            yield _format_sse_event(data, event_id)

            # Notify client about dropped events (slow consumer)
            warning = _drain_drop_counter(session.drop_lock, session.drop_counter)
            if warning:
                yield warning
    finally:
        streams.unsubscribe(session.operations, session.sub_ids)


def register(app: App, ctx: RouteContext) -> None:
    """Register the SSE subscribe routes."""
    operations = OperationContext(
        bus=ctx.bus,
        authz=ctx.authz,
        token_store=ctx.token_store,
        allow_unauthenticated=ctx.allow_unauthenticated,
    )
    cors = ctx.cors_origin

    async def _handle_subscribe(request: Request, ch_list: list[str]) -> StreamingResponse | tuple:
        """Shared SSE handler for all subscribe endpoints."""
        if not ch_list:
            return _error("Bad Request", "At least one channel required", 400)

        # Parse Last-Event-ID for cursor-based resume
        last_event_id = request.headers.get("last-event-id", "").strip() or None

        # Opt-in own-message filter, for consumers that do not use the
        # SDK.  Compared against the authenticated identity, never a
        # client-supplied user id; no effect for supertoken/no-auth.
        exclude_own_param = (request.query_params.get("exclude_own") or ["false"])[0]
        exclude_own = exclude_own_param.strip().lower() == "true"

        principal = request.state.principal
        user_id = principal.user_id
        try:
            q, sub_ids, drop_lock, drop_counter = await _setup_sse_subscriptions(
                operations, ch_list, principal, exclude_own
            )
        except OperationError as err:
            return _operation_error(err)

        on_activity = partial(ctx.note_activity, user_id) if user_id is not None else None

        return StreamingResponse(
            _sse_event_generator(
                _SSESession(
                    queue=q,
                    sub_ids=sub_ids,
                    operations=operations,
                    channels=ch_list,
                    principal=principal,
                    last_event_id=last_event_id,
                    drop_lock=drop_lock,
                    drop_counter=drop_counter,
                    exclude_own=exclude_own,
                    on_activity=on_activity,
                )
            ),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": cors,
            },
        )

    @app.get("/v1/subscribe")
    async def subscribe(request: Request) -> StreamingResponse | tuple:
        """Multi-channel SSE endpoint.

        Accepts repeated ``channel`` params and/or a comma-separated
        ``channels`` param::

            GET /v1/subscribe?channel=ch1&channel=ch2
            GET /v1/subscribe?channels=ch1,ch2

        ``exclude_own=true`` (default false) drops messages sent by the
        authenticated caller.  The per-channel endpoint accepts the same
        flag.
        """
        ch_list = list(request.query_params.get("channel", []))
        # Also accept comma-separated "channels" param
        for csv in request.query_params.get("channels", []):
            ch_list.extend(c.strip() for c in csv.split(",") if c.strip())
        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for ch in ch_list:
            if ch not in seen:
                seen.add(ch)
                deduped.append(ch)
        return await _handle_subscribe(request, deduped)

    @app.get("/v1/channels/<channel>/subscribe")
    async def subscribe_channel(request: Request, channel: str = "") -> StreamingResponse | tuple:
        """Per-channel SSE convenience endpoint."""
        if not channel:
            return _error("Bad Request", "Channel name required", 400)
        return await _handle_subscribe(request, [channel])
