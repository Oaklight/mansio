"""Federation support for cross-instance communication.

Provides ``FederationLink`` — a client-side component that connects
two mansio instances for channel replication and on-demand routing.
No server-side changes required.

Example::

    local = MansioClient("http://instance-a:8742", "agent-a", token="mst-xxx")
    remote = MansioClient("http://instance-b:8742", "agent-b", token="mst-yyy")

    link = FederationLink(local, remote, local_instance="a", remote_instance="b")

    # Replicate a channel bidirectionally
    link.replicate(["group:shared-project"])

    # On-demand read from remote
    msgs = link.route_read("broadcast:releases")

    # Clean up
    link.close()
"""

from __future__ import annotations

import logging
import threading
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mansio_client.client import MansioClient
    from mansio_client.types import Message

logger = logging.getLogger(__name__)

# Valid replication modes
_VALID_MODES = frozenset({"bidirectional", "pull", "push"})

# How long a worker waits for an SSE wake-up before polling anyway.
# The poll is the safety net for anything SSE misses (a subscription
# that died without reconnecting, a message published while the worker
# was backing off).
_DEFAULT_POLL_INTERVAL = 15.0

# Backoff bounds for a failed forward. The first retry is quick so a
# brief target blip costs little; repeated failures back off so an
# extended outage does not hammer a dead server.
_DEFAULT_RETRY_BACKOFF = 1.0
_DEFAULT_MAX_RETRY_BACKOFF = 60.0

# How many of the target channel's newest messages to scan when
# recovering the replication cursor at startup.
_DEFAULT_RESUME_SCAN = 1000

# Messages fetched from the source per catch-up request.
_CATCHUP_PAGE = 200


class _ReplicationStream:
    """One-way replication of a single channel, source → target.

    Every message is delivered by :meth:`_drain`, which asks the source
    for everything after the replication cursor and forwards it in
    order. The SSE subscription is only a wake-up signal: it makes
    delivery prompt, but it is never the thing that carries a message.
    A dropped subscription, a failed forward, or a bridge that was not
    running therefore costs latency instead of losing messages.

    The cursor — the last source message ID successfully written to the
    target — is recovered from the target's own history rather than
    from a side table. Each bridged copy already records the source
    message's ``original_id`` in its metadata, so the newest bridged
    copy on the target *is* the durable cursor. Deriving it this way
    cannot disagree with what was actually written, which a separately
    persisted cursor can whenever a write and a cursor update do not
    both land.
    """

    def __init__(
        self,
        *,
        source: MansioClient,
        target: MansioClient,
        channel: str,
        source_instance: str,
        target_instance: str,
        poll_interval: float,
        retry_backoff: float,
        max_retry_backoff: float,
        resume_scan: int,
    ) -> None:
        self._source = source
        self._target = target
        self._channel = channel
        self._source_instance = source_instance
        self._target_instance = target_instance
        self._poll_interval = poll_interval
        self._retry_backoff = retry_backoff
        self._max_retry_backoff = max_retry_backoff
        self._resume_scan = resume_scan

        self._cursor: str | None = None
        self._resumed = False
        # Source IDs already present on the target, collected while
        # recovering the cursor. Guards against bridged copies that
        # landed out of order, which the cursor alone would not catch.
        self._seen: set[str] = set()

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sub_id: str | None = None

    @property
    def direction(self) -> str:
        """Human-readable direction label, used in log messages."""
        return f"{self._source_instance} -> {self._target_instance}"

    def start(self) -> None:
        """Subscribe on the source and start the delivery worker."""
        self._sub_id = self._source.subscribe(self._channel, self._on_message)
        self._thread = threading.Thread(
            target=self._run,
            name=f"mansio-federation-{self._channel}-{self._source_instance}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Unsubscribe and shut the worker down."""
        self._stop.set()
        self._wake.set()
        if self._sub_id is not None:
            try:
                self._source.unsubscribe(self._sub_id)
            except Exception as exc:
                # Stopping is best-effort: the subscription may already
                # be gone, or the source unreachable. The worker still
                # exits, so replication stops either way.
                logger.warning(
                    "federation[%s %s]: unsubscribe failed (%s: %s)",
                    self._channel,
                    self.direction,
                    type(exc).__name__,
                    exc,
                )
            self._sub_id = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ── Worker ────────────────────────────────────────────────

    def _on_message(self, msg: Message) -> None:
        """SSE callback: wake the worker, which does the forwarding.

        Nothing here can fail, so the transport's handling of callback
        exceptions is irrelevant to delivery.
        """
        self._wake.set()

    def _run(self) -> None:
        backoff = self._retry_backoff
        while not self._stop.is_set():
            self._wake.clear()
            # ``stop()`` raises the stop flag before the wake-up, so
            # re-checking here catches a stop whose wake-up the clear
            # above just discarded.
            if self._stop.is_set():
                return
            try:
                self._drain()
            except Exception as exc:
                logger.warning(
                    "federation[%s %s]: replication failed (%s: %s); retrying in %.1fs",
                    self._channel,
                    self.direction,
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self._max_retry_backoff)
                continue
            backoff = self._retry_backoff
            self._wake.wait(self._poll_interval)

    def _drain(self) -> None:
        """Forward everything on the source after the cursor."""
        if not self._resumed:
            self._resume()
        while not self._stop.is_set():
            batch = self._source.channel_read(
                self._channel,
                limit=_CATCHUP_PAGE,
                after=self._cursor,
                order="oldest",
            )
            if not batch:
                return
            for msg in batch:
                if self._stop.is_set():
                    return
                self._forward(msg)
                # Advance only after the write succeeded; a raised
                # exception leaves the cursor on the last delivered
                # message so the retry resumes from there.
                self._cursor = msg.id
            if len(batch) < _CATCHUP_PAGE:
                return

    def _resume(self) -> None:
        """Recover the replication cursor from the target's history."""
        existing = self._target.channel_read(
            self._channel,
            limit=self._resume_scan,
            order="newest",
        )
        for msg in existing:
            meta = msg.metadata or {}
            if not meta.get("bridged"):
                continue
            if meta.get("source_instance") != self._source_instance:
                continue
            original_id = meta.get("original_id")
            if not isinstance(original_id, str):
                continue
            self._seen.add(original_id)
            if self._cursor is None or original_id > self._cursor:
                self._cursor = original_id

        if self._cursor is None and len(existing) >= self._resume_scan:
            # The scan window was full and held no bridged copy, so an
            # older one may exist beyond it. Replicating from the start
            # of the source channel is the safe choice, at the cost of
            # re-sending copies the scan could not see.
            logger.warning(
                "federation[%s %s]: no replicated message found in the newest "
                "%d messages on the target; replicating from the start of the "
                "source channel, which may duplicate older copies",
                self._channel,
                self.direction,
                self._resume_scan,
            )
        self._resumed = True
        logger.info(
            "federation[%s %s]: resuming after %s",
            self._channel,
            self.direction,
            self._cursor or "the start of the channel",
        )

    def _forward(self, msg: Message) -> None:
        """Write one source message to the target."""
        meta = msg.metadata or {}
        # Anti-loop: a message that arrived here by bridging is not
        # bridged onward. This boolean approach prevents infinite loops
        # between two instances, but intentionally does **not** support
        # multi-hop mesh routing (A → B → C): a message bridged from A
        # to B will not be forwarded onward to C. Mesh topologies
        # require a ``visited_instances`` list instead of a boolean.
        if meta.get("bridged"):
            return
        if msg.id in self._seen:
            return

        self._target.channel_send(
            msg.channel,
            msg.payload,
            msg_type=msg.msg_type,
            metadata={
                **meta,
                "bridged": True,
                "source_instance": self._source_instance,
                "original_id": msg.id,
                "original_sender": msg.sender,
                "attributed_sender": f"{msg.sender}@{self._source_instance}",
            },
        )


class FederationLink:
    """Connect two mansio instances for replication and routing.

    Args:
        local: MansioClient connected to the local instance.
        remote: MansioClient connected to the remote instance.
        local_instance: String identifier for the local instance.
        remote_instance: String identifier for the remote instance.
        poll_interval: Seconds a replication worker waits for an SSE
            event before checking the source anyway.
        retry_backoff: Seconds before the first retry of a failed
            forward. Doubles per consecutive failure.
        max_retry_backoff: Upper bound on the retry delay.
        resume_scan: How many of the target channel's newest messages
            to scan when recovering a replication cursor on startup.
    """

    def __init__(
        self,
        local: MansioClient,
        remote: MansioClient,
        *,
        local_instance: str = "local",
        remote_instance: str = "remote",
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        retry_backoff: float = _DEFAULT_RETRY_BACKOFF,
        max_retry_backoff: float = _DEFAULT_MAX_RETRY_BACKOFF,
        resume_scan: int = _DEFAULT_RESUME_SCAN,
    ) -> None:
        warnings.warn(
            "FederationLink is experimental: two-instance bridging only, "
            "no multi-hop mesh, loop prevention via metadata convention "
            "(not server-enforced). API may change without notice.",
            FutureWarning,
            stacklevel=2,
        )
        if local_instance == remote_instance:
            raise ValueError(
                f"local_instance and remote_instance must differ, "
                f"got {local_instance!r} for both"
            )
        self._local = local
        self._remote = remote
        self._local_instance = local_instance
        self._remote_instance = remote_instance
        self._poll_interval = poll_interval
        self._retry_backoff = retry_backoff
        self._max_retry_backoff = max_retry_backoff
        self._resume_scan = resume_scan

        # channel -> {"mode": str, "streams": list[_ReplicationStream]}
        self._replications: dict[str, dict] = {}
        self._lock = threading.Lock()

    @property
    def local_instance(self) -> str:
        """Identifier for the local instance."""
        return self._local_instance

    @property
    def remote_instance(self) -> str:
        """Identifier for the remote instance."""
        return self._remote_instance

    # ── Mode 1: Replication ───────────────────────────────────

    def replicate(
        self,
        channels: list[str],
        mode: str = "bidirectional",
    ) -> None:
        """Start continuous channel replication between instances.

        Replication resumes from whatever is already on the receiving
        side: each direction catches up on messages published while the
        bridge was down or the receiving instance was unreachable, then
        keeps following the channel. A direction with nothing replicated
        yet starts from the beginning of the source channel.

        Catch-up happens in the background, so this returns before the
        two sides are in sync.

        Args:
            channels: Channel names to replicate.
            mode: Replication direction.
                ``"bidirectional"`` — sync both ways.
                ``"pull"`` — remote → local only.
                ``"push"`` — local → remote only.

        Raises:
            ValueError: If mode is invalid or a channel is already
                being replicated.
        """
        if mode not in _VALID_MODES:
            raise ValueError(
                f"Invalid replication mode {mode!r}. "
                f"Must be one of: {', '.join(sorted(_VALID_MODES))}"
            )

        with self._lock:
            for ch in channels:
                if ch in self._replications:
                    raise ValueError(
                        f"Channel {ch!r} is already being replicated. "
                        f"Call stop_replication() first."
                    )

            for ch in channels:
                streams: list[_ReplicationStream] = []

                if mode in ("bidirectional", "pull"):
                    streams.append(
                        self._make_stream(
                            ch,
                            source=self._remote,
                            target=self._local,
                            source_instance=self._remote_instance,
                            target_instance=self._local_instance,
                        )
                    )

                if mode in ("bidirectional", "push"):
                    streams.append(
                        self._make_stream(
                            ch,
                            source=self._local,
                            target=self._remote,
                            source_instance=self._local_instance,
                            target_instance=self._remote_instance,
                        )
                    )

                for stream in streams:
                    stream.start()

                self._replications[ch] = {"mode": mode, "streams": streams}

    def stop_replication(self, channels: list[str] | None = None) -> None:
        """Stop replication for the given channels, or all if None.

        Args:
            channels: Channels to stop replicating. If None, stops all.
        """
        with self._lock:
            targets = channels if channels is not None else list(self._replications)
            for ch in targets:
                info = self._replications.pop(ch, None)
                if info is None:
                    continue
                for stream in info["streams"]:
                    stream.stop()

    @property
    def replicating(self) -> dict[str, str]:
        """Currently replicated channels mapped to their mode."""
        with self._lock:
            return {ch: info["mode"] for ch, info in self._replications.items()}

    # ── Mode 2: Federated Routing ─────────────────────────────

    def route_read(
        self,
        channel: str,
        limit: int = 10,
        **kwargs,
    ) -> list[Message]:
        """Read messages from a channel on the remote instance.

        This is a stateless proxy — no local copy is made.

        Args:
            channel: Remote channel to read from.
            limit: Maximum messages to return.
            **kwargs: Additional arguments passed to
                ``MansioClient.channel_read()``.

        Returns:
            Messages from the remote channel.
        """
        return self._remote.channel_read(channel, limit=limit, **kwargs)

    def route_send(
        self,
        channel: str,
        content: str,
        msg_type: str = "chat",
        **kwargs,
    ) -> str:
        """Send a message to a channel on the remote instance.

        Args:
            channel: Remote channel to send to.
            content: Message content.
            msg_type: Message type (default ``"chat"``).
            **kwargs: Additional arguments passed to
                ``MansioClient.channel_send()``.

        Returns:
            Message ID from the remote instance.
        """
        return self._remote.channel_send(channel, content, msg_type=msg_type, **kwargs)

    # ── Lifecycle ─────────────────────────────────────────────

    def close(self) -> None:
        """Stop all replication and clean up."""
        self.stop_replication()

    def __enter__(self) -> FederationLink:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        n = len(self._replications)
        return (
            f"FederationLink("
            f"{self._local_instance!r} <-> {self._remote_instance!r}, "
            f"replicating={n} channels)"
        )

    # ── Internal ──────────────────────────────────────────────

    def _make_stream(
        self,
        channel: str,
        *,
        source: MansioClient,
        target: MansioClient,
        source_instance: str,
        target_instance: str,
    ) -> _ReplicationStream:
        return _ReplicationStream(
            source=source,
            target=target,
            channel=channel,
            source_instance=source_instance,
            target_instance=target_instance,
            poll_interval=self._poll_interval,
            retry_backoff=self._retry_backoff,
            max_retry_backoff=self._max_retry_backoff,
            resume_scan=self._resume_scan,
        )
