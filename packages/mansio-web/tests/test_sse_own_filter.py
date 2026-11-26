"""Server-side own-message filter for SSE subscribers (#242, item 2).

``GET /v1/subscribe`` gains an opt-in ``exclude_own=true`` query flag for
consumers that do not use the SDK.  Filtering uses the authenticated
identity (``request.state.auth_result``), never a client-supplied user id,
and applies to live delivery and Last-Event-ID replay alike.  A supertoken
or a no-auth connection has no bound user, so the flag has no effect there.

The SDK default and ``channel_read``/``query`` are deliberately untouched.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse

from mansio_web._vendor.sse import SSEClient


class _Collector:
    """Read an SSE stream in the background and record decoded events."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        last_event_id: str = "",
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._client = SSEClient(
            url,
            headers=headers,
            timeout=2,
            max_retries=0,
            last_event_id=last_event_id,
        )
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            for event in self._client:
                self.events.append(json.loads(event.data))
                if self._stop.is_set():
                    break
        except Exception:
            pass
        finally:
            self._client.close()

    def __enter__(self) -> _Collector:
        self._thread.start()
        time.sleep(0.5)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._client.close()
        self._thread.join(timeout=2)

    def senders(self) -> list[str]:
        return [e["message"]["sender"] for e in self.events]

    def wait_for_sender(self, sender: str, timeout: float = 5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if sender in self.senders():
                return True
            time.sleep(0.05)
        return False


def _req(url: str, path: str, token: str | None = None, body: dict | None = None):
    parsed = urllib.parse.urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    raw = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if raw else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("POST" if body is not None else "GET", path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode() or "{}")
    status = resp.status
    conn.close()
    return status, data


class TestDefaultKeepsOwnMessages:
    def test_scoped_token_sees_its_own_messages_by_default(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        alice = store.create_token(user_id="alice", label="a")["token"]

        with _Collector(f"{url}/v1/subscribe?channel=general", token=alice) as sub:
            bus.publish("general", "alice", "chat", "from alice")
            bus.publish("general", "bob", "chat", "from bob")
            assert sub.wait_for_sender("bob")

        assert sub.senders() == ["alice", "bob"]


class TestExcludeOwn:
    def test_flag_drops_the_subscribers_own_messages(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        alice = store.create_token(user_id="alice", label="a")["token"]

        with _Collector(f"{url}/v1/subscribe?channel=general&exclude_own=true", token=alice) as sub:
            bus.publish("general", "alice", "chat", "own first")
            bus.publish("general", "bob", "chat", "from bob")
            assert sub.wait_for_sender("bob")
            # A later own message must not arrive either.
            bus.publish("general", "alice", "chat", "own second")
            time.sleep(0.5)

        assert sub.senders() == ["bob"]

    def test_other_agents_messages_are_unaffected(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        bob = store.create_token(user_id="bob", label="b")["token"]

        with _Collector(f"{url}/v1/subscribe?channel=general&exclude_own=true", token=bob) as sub:
            bus.publish("general", "alice", "chat", "from alice")
            bus.publish("general", "bob", "chat", "from bob")
            assert sub.wait_for_sender("alice")
            time.sleep(0.3)

        assert sub.senders() == ["alice"]

    def test_flag_applies_to_last_event_id_replay(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        alice = store.create_token(user_id="alice", label="a")["token"]

        first = bus.publish("general", "alice", "chat", "one")
        second = bus.publish("general", "bob", "chat", "two")
        bus.publish("general", "alice", "chat", "three")

        with _Collector(
            f"{url}/v1/subscribe?channel=general&exclude_own=true",
            token=alice,
            last_event_id=first,
        ) as sub:
            assert sub.wait_for_sender("bob")

        # Replay skips alice's own "three"; only bob's "two" is replayed.
        assert sub.senders() == ["bob"]
        assert sub.events[0]["message"]["id"] == second

    def test_flag_does_not_filter_query_or_read(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        alice = store.create_token(user_id="alice", label="a")["token"]
        own = bus.publish("general", "alice", "chat", "own")
        bus.publish("general", "bob", "chat", "other")

        status, data = _req(url, "/v1/query?channel=general", alice)

        assert status == 200, data
        ids = [m["id"] for m in data["messages"]]
        assert own in ids
        assert len(ids) == 2


class TestNoBoundUser:
    def test_supertoken_filter_is_a_no_op(self, mansio_server) -> None:
        """A supertoken has no user id, so there is nothing to exclude."""
        url, store, bus, _server = mansio_server
        supertoken = store.create_token(user_id=None, label="super")["token"]

        with _Collector(
            f"{url}/v1/subscribe?channel=general&exclude_own=true", token=supertoken
        ) as sub:
            bus.publish("general", "alice", "chat", "from alice")
            bus.publish("general", "bob", "chat", "from bob")
            assert sub.wait_for_sender("bob")
            time.sleep(0.2)

        assert sub.senders() == ["alice", "bob"]

    def test_no_auth_mode_filter_is_a_no_op(self, server_url) -> None:
        with _Collector(f"{server_url}/v1/subscribe?channel=general&exclude_own=true") as sub:
            for sender in ("alice", "bob"):
                status, data = _req(
                    server_url,
                    "/v1/publish",
                    None,
                    {
                        "channel": "general",
                        "sender": sender,
                        "msg_type": "chat",
                        "payload": f"from {sender}",
                    },
                )
                assert status == 200, data
            assert sub.wait_for_sender("bob")
            time.sleep(0.2)

        assert sub.senders() == ["alice", "bob"]
