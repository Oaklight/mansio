"""HTTP transport for MansioClient — connects to an HttpFrontend server.

Uses vendored zerodep modules:
- ``_vendor.httpclient`` for HTTP requests (supports HTTP + HTTPS)
- ``_vendor.sse`` for SSE streaming (auto-reconnect, W3C-compliant parser)
"""

from __future__ import annotations

import contextlib
import json
import threading
import urllib.parse
from collections.abc import Callable
from typing import Literal, overload

from __PKG__._vendor.httpclient import Client as HttpClient
from __PKG__._vendor.sse import SSEClient
from __PKG__.types import AgentPresence, ClaimResult, Message


class MansioAPIError(Exception):
    """Raised when the Mansio server returns a non-2xx HTTP response.

    Attributes:
        status_code: The HTTP status code from the server.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


class HttpTransport:
    """Client-side transport that talks to a remote HttpFrontend.

    Implements the Transport protocol for MansioClient, enabling
    remote agent communication over HTTP/HTTPS.

    Args:
        base_url: Server URL, e.g. ``"http://localhost:8742"`` or
            ``"https://mansio.example.com"``.
        agent_id: Agent identifier for SSE subscriptions.
        timeout: HTTP request timeout in seconds.
        token: Optional Bearer token for API authentication (``mst-...``).
    """

    def __init__(
        self,
        base_url: str,
        agent_id: str = "",
        timeout: float = 10.0,
        token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_id = agent_id
        self._timeout = timeout
        self._token = token

        # Shared HTTP client (thread-safe, connection pooling)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = HttpClient(headers=headers, timeout=timeout)

        # SSE state
        self._sse_client: SSEClient | None = None
        self._sse_thread: threading.Thread | None = None
        self._sse_channels: set[str] = set()
        self._sse_callbacks: dict[str, dict[str, Callable]] = {}
        self._sse_lock = threading.Lock()
        self._sse_stop = threading.Event()
        self._sub_counter = 0
        self._last_event_id: str = ""  # track across SSE restarts

    # ── Transport Protocol ────────────────────────────────────────

    def publish(
        self,
        channel: str,
        sender: str,
        msg_type: str,
        payload: str,
        metadata: dict | None = None,
        *,
        queue: bool = False,
        parent_id: str | None = None,
        intent: str | None = None,
    ) -> str:
        """Publish a message via the remote server.

        Returns:
            The message ID assigned by the server.
        """
        body: dict = {
            "channel": channel,
            "sender": sender,
            "msg_type": msg_type,
            "payload": payload,
        }
        if metadata:
            body["metadata"] = metadata
        if queue:
            body["queue"] = True
        if parent_id:
            body["parent_id"] = parent_id
        if intent:
            body["intent"] = intent

        resp = self._http.post(f"{self._base_url}/v1/publish", json=body)
        self._check_response(resp)
        return resp.json()["message_id"]

    def queue_claim(
        self, channel: str, claimed_by: str, *, lease_seconds: int = 300
    ) -> ClaimResult | None:
        """Claim the oldest unclaimed/lease-expired message."""
        body: dict = {"channel": channel, "claimed_by": claimed_by}
        if lease_seconds != 300:
            body["lease_seconds"] = lease_seconds
        resp = self._http.post(f"{self._base_url}/v1/queue/claim", json=body)
        self._check_response(resp)
        data = resp.json()
        if not data.get("claimed"):
            return None
        r = data["result"]
        return ClaimResult(
            message=self._dict_to_msg(r["message"]),
            status=r["status"],
            claimed_by=r["claimed_by"],
            claimed_at=r["claimed_at"],
        )

    def queue_ack(self, message_id: str, claimed_by: str) -> ClaimResult | None:
        """Acknowledge a claimed message as completed."""
        resp = self._http.post(
            f"{self._base_url}/v1/queue/ack",
            json={"message_id": message_id, "claimed_by": claimed_by},
        )
        self._check_response(resp)
        data = resp.json()
        if not data.get("acked"):
            return None
        r = data["result"]
        return ClaimResult(
            message=self._dict_to_msg(r["message"]),
            status=r["status"],
            claimed_by=r["claimed_by"],
            claimed_at=r["claimed_at"],
        )

    def queue_status(self, message_id: str) -> dict | None:
        """Return the queue status dict for a single message.

        Returns:
            Dict with 'status', 'claimed_by', 'claimed_at', etc.,
            or None if the message has no queue status.
        """
        params = urllib.parse.urlencode({"message_id": message_id})
        resp = self._http.get(f"{self._base_url}/v1/queue/status?{params}")
        if resp.status_code == 404:
            return None
        self._check_response(resp)
        return resp.json().get("status")

    def query(
        self,
        channel: str,
        after: str | None = None,
        limit: int = 100,
        msg_type: str | None = None,
        order: Literal["oldest", "newest"] = "oldest",
        thread_id: str | None = None,
        intent: str | None = None,
        offset: int = 0,
    ) -> list[Message]:
        """Query messages from a channel via the remote server."""
        params: dict[str, str] = {"channel": channel, "limit": str(limit)}
        if after:
            params["after"] = after
        if msg_type:
            params["msg_type"] = msg_type
        if order != "oldest":
            params["order"] = order
        if thread_id:
            params["thread_id"] = thread_id
        if intent:
            params["intent"] = intent
        if offset:
            params["offset"] = str(offset)

        url = f"{self._base_url}/v1/query?{urllib.parse.urlencode(params)}"
        resp = self._http.get(url)
        self._check_response(resp)
        return [self._dict_to_msg(m) for m in resp.json().get("messages", [])]

    @overload
    def channels(self) -> list[str]: ...
    @overload
    def channels(self, *, detail: Literal[False]) -> list[str]: ...
    @overload
    def channels(self, *, detail: Literal[True]) -> list[dict]: ...

    def channels(self, *, detail: bool = False) -> list[str] | list[dict]:
        """List all channels on the remote server.

        Args:
            detail: If True, return list of dicts with channel metadata
                instead of plain channel name strings.

        Returns:
            List of channel name strings, or list of dicts when
            *detail* is True.
        """
        url = f"{self._base_url}/v1/channels"
        if detail:
            url += "?detail=true"
        resp = self._http.get(url)
        self._check_response(resp)
        return resp.json().get("channels", [])

    @property
    def require_auth(self) -> bool:
        """Whether the remote bus requires authentication."""
        if self._require_auth is None:
            resp = self._http.get(f"{self._base_url}/v1/auth/check")
            self._check_response(resp)
            self._require_auth = resp.json().get("require_auth", False)
        return self._require_auth

    # ── Presence ──────────────────────────────────────────────────

    def heartbeat(self, agent_id: str, metadata: dict | None = None) -> None:
        """Record a heartbeat for *agent_id*."""
        body: dict = {"agent_id": agent_id}
        if metadata:
            body["metadata"] = metadata
        resp = self._http.post(f"{self._base_url}/v1/presence/heartbeat", json=body)
        self._check_response(resp)

    def agents(self, timeout_seconds: int = 120) -> list[AgentPresence]:
        """Return all known agents with computed online/offline status."""
        params = urllib.parse.urlencode({"timeout": str(timeout_seconds)})
        resp = self._http.get(f"{self._base_url}/v1/presence?{params}")
        self._check_response(resp)
        return [
            AgentPresence(
                agent_id=a["agent_id"],
                status=a["status"],
                last_seen=a["last_seen"],
                metadata=a.get("metadata"),
            )
            for a in resp.json().get("agents", [])
        ]

    def agent_status(
        self, agent_id: str, timeout_seconds: int = 120
    ) -> AgentPresence | None:
        """Return presence for a single agent, or ``None`` if unknown."""
        params = urllib.parse.urlencode({"timeout": str(timeout_seconds)})
        resp = self._http.get(
            f"{self._base_url}/v1/presence/{urllib.parse.quote(agent_id, safe='')}?{params}"
        )
        if resp.status_code == 404:
            return None
        self._check_response(resp)
        data = resp.json()
        return AgentPresence(
            agent_id=data["agent_id"],
            status=data["status"],
            last_seen=data["last_seen"],
            metadata=data.get("metadata"),
        )

    def close(self) -> None:
        """Stop SSE thread and release resources."""
        self._sse_stop.set()
        if self._sse_client:
            self._sse_client.close()
        if self._sse_thread and self._sse_thread.is_alive():
            self._sse_thread.join(timeout=3)
        self._http.close()

    # ── SSE Subscription ─────────────────────────────────────────

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Message], None],
    ) -> str:
        """Subscribe to real-time notifications via SSE.

        The callback fires in the SSE background thread when a
        new message arrives on the channel.

        Args:
            channel: Channel to subscribe to.
            callback: Function called with each new Message.

        Returns:
            Subscription ID for unsubscribe().
        """
        with self._sse_lock:
            self._sub_counter += 1
            sub_id = f"http-sub-{self._sub_counter}"

            if channel not in self._sse_callbacks:
                self._sse_callbacks[channel] = {}
            self._sse_callbacks[channel][sub_id] = callback

            needs_restart = channel not in self._sse_channels
            self._sse_channels.add(channel)

        if needs_restart:
            self._restart_sse()

        return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription.

        Args:
            subscription_id: ID returned by subscribe().
        """
        with self._sse_lock:
            for channel, subs in list(self._sse_callbacks.items()):
                if subscription_id in subs:
                    del subs[subscription_id]
                    if not subs:
                        del self._sse_callbacks[channel]
                        self._sse_channels.discard(channel)
                    break

    # ── SSE Background Thread ─────────────────────────────────────

    def _restart_sse(self) -> None:
        """(Re)start the SSE background thread with current channels."""
        self._sse_stop.set()
        if self._sse_client:
            self._sse_client.close()
        if self._sse_thread and self._sse_thread.is_alive():
            self._sse_thread.join(timeout=3)

        self._sse_stop = threading.Event()
        self._sse_thread = threading.Thread(
            target=self._sse_loop, daemon=True, name="mansio-sse"
        )
        self._sse_thread.start()

    def _sse_loop(self) -> None:
        """Background loop: connect to SSE endpoint via zerodep SSEClient."""
        with self._sse_lock:
            channels = list(self._sse_channels)
        if not channels:
            return

        url = self._build_sse_url(channels)
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            self._sse_client = SSEClient(
                url,
                headers=headers,
                timeout=self._timeout + 20,  # longer than keepalive interval
                max_retries=-1,  # unlimited reconnect
                last_event_id=self._last_event_id,
            )
            for event in self._sse_client:
                if self._sse_stop.is_set():
                    break
                if event.id:
                    self._last_event_id = event.id
                self._dispatch_sse_event(event.data)
        except Exception:
            pass  # SSEClient handles reconnection internally
        finally:
            if self._sse_client:
                self._sse_client.close()
                self._sse_client = None

    def _build_sse_url(self, channels: list[str]) -> str:
        """Build the full SSE subscribe URL."""
        params = urllib.parse.urlencode(
            [("channel", ch) for ch in channels] + [("agent_id", self._agent_id)]
        )
        return f"{self._base_url}/v1/subscribe?{params}"

    def _dispatch_sse_event(self, data: str) -> None:
        """Parse SSE event data and dispatch to callbacks."""
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            return

        channel = event.get("channel")
        msg_data = event.get("message")
        if not channel or not msg_data:
            return

        msg = self._dict_to_msg(msg_data)

        with self._sse_lock:
            cbs = list((self._sse_callbacks.get(channel) or {}).values())

        for cb in cbs:
            with contextlib.suppress(Exception):
                cb(msg)

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _check_response(resp) -> None:
        """Raise MansioAPIError if the response indicates a failure."""
        if resp.status_code >= 400:
            try:
                error = resp.json() if resp.content else {}
            except Exception:
                error = {}
            raise MansioAPIError(
                resp.status_code,
                error.get("message", "Request failed")
                if isinstance(error, dict)
                else "Request failed",
            )

    @staticmethod
    def _dict_to_msg(d: dict) -> Message:
        return Message(
            id=d["id"],
            channel=d["channel"],
            sender=d["sender"],
            msg_type=d["msg_type"],
            payload=d["payload"],
            timestamp=d["timestamp"],
            metadata=d.get("metadata"),
            parent_id=d.get("parent_id"),
            thread_id=d.get("thread_id"),
            intent=d.get("intent"),
        )

    def __repr__(self) -> str:
        auth = "auth" if self._token else "no-auth"
        return f"HttpTransport({self._base_url!r}, {auth})"
