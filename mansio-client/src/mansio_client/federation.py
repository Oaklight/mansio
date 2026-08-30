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

import threading
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mansio_client.client import MansioClient
    from mansio_client.types import Message

# Valid replication modes
_VALID_MODES = frozenset({"bidirectional", "pull", "push"})


class FederationLink:
    """Connect two mansio instances for replication and routing.

    Args:
        local: MansioClient connected to the local instance.
        remote: MansioClient connected to the remote instance.
        local_instance: String identifier for the local instance.
        remote_instance: String identifier for the remote instance.
    """

    def __init__(
        self,
        local: MansioClient,
        remote: MansioClient,
        *,
        local_instance: str = "local",
        remote_instance: str = "remote",
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

        # channel -> {"mode": str, "subs": list[tuple[str, MansioClient]]}
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
                # Track (sub_id, owning_client) tuples so we
                # unsubscribe from the correct client on stop.
                subs: list[tuple[str, MansioClient]] = []

                if mode in ("bidirectional", "pull"):
                    # remote → local
                    sub_id = self._remote.subscribe(
                        ch,
                        self._make_bridge_callback(
                            target=self._local,
                            source_instance=self._remote_instance,
                        ),
                    )
                    subs.append((sub_id, self._remote))

                if mode in ("bidirectional", "push"):
                    # local → remote
                    sub_id = self._local.subscribe(
                        ch,
                        self._make_bridge_callback(
                            target=self._remote,
                            source_instance=self._local_instance,
                        ),
                    )
                    subs.append((sub_id, self._local))

                self._replications[ch] = {"mode": mode, "subs": subs}

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
                for sub_id, client in info["subs"]:
                    try:
                        client.unsubscribe(sub_id)
                    except Exception:
                        pass

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

    def _make_bridge_callback(
        self,
        target: MansioClient,
        source_instance: str,
    ):
        """Create a callback that bridges messages to the target instance.

        The callback skips messages that already carry ``bridged=True``
        in their metadata (anti-loop).  This boolean approach prevents
        infinite loops between two instances, but intentionally does
        **not** support multi-hop mesh routing (A → B → C): a message
        bridged from A to B will not be forwarded onward to C.  Mesh
        topologies require a ``visited_instances`` list instead of a
        boolean; this is deferred to Phase 2.
        """

        def _bridge(msg: Message) -> None:
            # Anti-loop: skip messages that were already bridged
            if msg.metadata and msg.metadata.get("bridged"):
                return

            # Enrich metadata with bridging info
            metadata = {
                **(msg.metadata or {}),
                "bridged": True,
                "source_instance": source_instance,
                "original_id": msg.id,
                "original_sender": msg.sender,
                "attributed_sender": f"{msg.sender}@{source_instance}",
            }

            target.channel_send(
                msg.channel,
                msg.payload,
                msg_type=msg.msg_type,
                metadata=metadata,
            )

        return _bridge
