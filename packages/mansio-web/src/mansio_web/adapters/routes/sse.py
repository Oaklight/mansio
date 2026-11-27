"""Server-sent events routes and per-connection session helpers."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from mansio_core.authz import DM_NAME_EXPECTED
from mansio_core.authz import user_involved as _user_involved
from mansio_core.contracts import (
    ContractError,
    StreamEvent,
    parse_stream_event,
    stream_event_to_json,
)

from mansio_web._vendor.httpserver import App, StreamingResponse

from ._common import _error, _invalid_dm_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from mansio_core.bus import Bus
    from mansio_core.types import Message

    from mansio_web._vendor.httpserver import Request

    from . import RouteContext


@dataclass
class _SSESession:
    """Bundles per-connection SSE state to reduce parameter sprawl."""

    queue: asyncio.Queue[str | None]
    sub_ids: list[str]
    bus: Any
    channels: list[str]
    auth_result: Any
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


def _setup_sse_subscriptions(
    bus: Bus, ch_list: list[str], auth_result: Any, exclude_own: bool = False
) -> tuple[asyncio.Queue[str | None], list[str], threading.Lock, list[int]]:
    """Subscribe to bus channels and return (queue, subscription_ids, drop_lock, drop_counter).

    Bus callbacks are sync and may run on any thread (the publisher's
    thread). We use ``loop.call_soon_threadsafe`` to safely enqueue
    data into the asyncio.Queue from non-event-loop threads.

    Returns a ``[dropped_count]`` list (mutable single-element) and
    a lock protecting it, so the async generator can report and reset
    the counter.
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)
    sub_ids: list[str] = []
    drop_lock = threading.Lock()
    drop_counter: list[int] = [0]  # mutable single-element for cross-thread access

    for ch in ch_list:

        def _make_callback(target_ch: str) -> Any:
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

            def _cb(msg: Message) -> None:
                if isinstance(auth_result, str) and not _user_involved(
                    auth_result, msg.channel, msg.sender
                ):
                    return
                if exclude_own and msg.sender == auth_result:
                    return
                data = stream_event_to_json(StreamEvent(channel=target_ch, message=msg))
                loop.call_soon_threadsafe(_safe_enqueue, data)

            return _cb

        sid = bus.subscribe(ch, _make_callback(ch))
        sub_ids.append(sid)

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


def _drain_drop_counter(
    drop_lock: threading.Lock | None,
    drop_counter: list[int] | None,
) -> str:
    """Return an SSE warning comment if events were dropped, else empty string.

    The warning is emitted after the next live event or on the next
    keepalive tick, whichever comes first, so a stream that goes quiet
    after drops still reports them within the keepalive interval.
    """
    if drop_lock is None or drop_counter is None:
        return ""
    with drop_lock:
        dropped = drop_counter[0]
        drop_counter[0] = 0
    if dropped:
        return f": warning: {dropped} event(s) dropped (slow consumer), re-query for gaps\n\n"
    return ""


async def _replay_missed_events(session: _SSESession) -> Any:
    """Yield SSE events for messages published after ``last_event_id``.

    When a channel's replay fills ``_SSE_REPLAY_LIMIT``, a warning comment
    precedes that channel's events.  The query cannot tell whether more
    messages exist beyond the limit, so the client should re-query; the
    warning can be a false positive when exactly the limit is waiting.
    """
    for ch in session.channels:
        missed = await asyncio.to_thread(
            session.bus.query, ch, after=session.last_event_id, limit=_SSE_REPLAY_LIMIT
        )
        if len(missed) >= _SSE_REPLAY_LIMIT:
            yield (
                f": warning: replay truncated at {_SSE_REPLAY_LIMIT} "
                f"for channel '{ch}', re-query for more\n\n"
            )
        if isinstance(session.auth_result, str):
            missed = [m for m in missed if _user_involved(session.auth_result, m.channel, m.sender)]
            if session.exclude_own:
                missed = [m for m in missed if m.sender != session.auth_result]
        for m in missed:
            yield _format_sse_event(stream_event_to_json(StreamEvent(channel=ch, message=m)), m.id)


async def _sse_event_generator(session: _SSESession) -> Any:
    """Async generator that yields SSE events from the queue.

    If *last_event_id* is set (via ``Last-Event-ID`` header), replays
    missed messages from the bus before switching to live streaming.
    Each event includes an ``id:`` field (the message ID) so the
    W3C SSE client can resume on reconnect.

    When events are dropped due to slow consumption, a warning SSE
    comment is emitted after the next event or keepalive tick so the
    client knows to re-query for any gaps.  A replay that fills
    ``_SSE_REPLAY_LIMIT`` is also flagged as possibly truncated.
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
        for sid in session.sub_ids:
            session.bus.unsubscribe(sid)


def register(app: App, ctx: RouteContext) -> None:
    """Register the SSE subscribe routes."""
    bus = ctx.bus
    authz = ctx.authz
    cors = ctx.cors_origin

    async def _handle_subscribe(request: Request, ch_list: list[str]) -> StreamingResponse | tuple:
        """Shared SSE handler for all subscribe endpoints."""
        if not ch_list:
            return _error("Bad Request", "At least one channel required", 400)

        for ch in ch_list:
            if _invalid_dm_name(ch):
                return _error("Bad Request", f"{DM_NAME_EXPECTED} (got '{ch}')", 400)

        auth_result = request.state.auth_result
        if isinstance(auth_result, str):
            forbidden = [ch for ch in ch_list if not authz.can_read(auth_result, ch)]
            if forbidden:
                return _error(
                    "Forbidden",
                    f"Token for '{auth_result}' cannot subscribe to: {', '.join(forbidden)}",
                    403,
                )

        # Parse Last-Event-ID for cursor-based resume
        last_event_id = request.headers.get("last-event-id", "").strip() or None

        # Opt-in own-message filter, for consumers that do not use the
        # SDK.  Compared against the authenticated identity, never a
        # client-supplied user id; no effect for supertoken/no-auth.
        exclude_own_param = (request.query_params.get("exclude_own") or ["false"])[0]
        exclude_own = exclude_own_param.strip().lower() == "true"

        q, sub_ids, drop_lock, drop_counter = _setup_sse_subscriptions(
            bus, ch_list, auth_result, exclude_own
        )

        on_activity = (
            partial(ctx.note_activity, auth_result) if isinstance(auth_result, str) else None
        )

        return StreamingResponse(
            _sse_event_generator(
                _SSESession(
                    queue=q,
                    sub_ids=sub_ids,
                    bus=bus,
                    channels=ch_list,
                    auth_result=auth_result,
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
