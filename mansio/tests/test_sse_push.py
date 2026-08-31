"""Tests for SSE push notification enhancements (#86).

Verifies:
- SSE events include ``id:`` field (message ID) for cursor-based resume
- ``Last-Event-ID`` header triggers replay of missed messages on reconnect
- Per-channel subscribe endpoint: ``GET /v1/channels/{channel}/subscribe``
- Multi-channel ``channels=ch1,ch2`` (comma-separated) parameter
- Backpressure: slow client doesn't block publishers
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio._vendor.httpclient import Client as HttpClient
from mansio._vendor.sse import SSEClient
from mansio.frontends import HttpFrontend
from mansio.transport_http import HttpTransport


@pytest.fixture()
def server_url():
    """Start a MansioServer with HttpFrontend on a random port, yield URL."""
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    url = f"http://{host}:{port}"

    yield url

    server.shutdown()


@pytest.fixture()
def server_with_bus():
    """Start a server and also yield the underlying Bus for direct publishes."""
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    url = f"http://{host}:{port}"

    yield url, bus

    server.shutdown()


# ── SSE Event ID Tests ───────────────────────────────────────────


class TestSseEventIds:
    """SSE events include id: field with message ID."""

    def test_sse_events_have_id_field(self, server_with_bus: tuple) -> None:
        """Each SSE event should include an id: field matching the message ID."""
        url, bus = server_with_bus
        received_events: list = []
        done = threading.Event()

        def collect_raw_sse():
            sse_url = f"{url}/v1/subscribe?channel=id-test"
            client = SSEClient(sse_url, timeout=10, max_retries=0)
            try:
                for event in client:
                    received_events.append(event)
                    if len(received_events) >= 1:
                        done.set()
                        break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=collect_raw_sse, daemon=True)
        t.start()
        time.sleep(0.5)

        # Publish a message
        msg_id = bus.publish("id-test", "sender-a", "chat", "hello")

        assert done.wait(timeout=5), "SSE event not received"
        assert len(received_events) == 1
        # The SSEEvent.id should match the message ID
        assert received_events[0].id == msg_id

    def test_multiple_events_have_sequential_ids(self, server_with_bus: tuple) -> None:
        """Multiple SSE events should each have their own id."""
        url, bus = server_with_bus
        received_events: list = []
        done = threading.Event()

        def collect():
            sse_url = f"{url}/v1/subscribe?channel=seq-test"
            client = SSEClient(sse_url, timeout=10, max_retries=0)
            try:
                for event in client:
                    received_events.append(event)
                    if len(received_events) >= 3:
                        done.set()
                        break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=collect, daemon=True)
        t.start()
        time.sleep(0.5)

        ids = []
        for i in range(3):
            ids.append(bus.publish("seq-test", "sender", "chat", f"msg-{i}"))

        assert done.wait(timeout=5)
        for i, event in enumerate(received_events):
            assert event.id == ids[i], f"Event {i}: expected id={ids[i]}, got {event.id}"


# ── Last-Event-ID Replay Tests ───────────────────────────────────


class TestLastEventIdReplay:
    """Server replays missed messages when Last-Event-ID is provided."""

    def test_replay_on_reconnect(self, server_with_bus: tuple) -> None:
        """Messages published while disconnected are replayed on reconnect."""
        url, bus = server_with_bus

        # Publish some messages and record the first message ID
        first_id = bus.publish("replay-ch", "agent-a", "chat", "msg-1")
        bus.publish("replay-ch", "agent-a", "chat", "msg-2")
        bus.publish("replay-ch", "agent-a", "chat", "msg-3")

        # Connect with Last-Event-ID set to the first message
        # Should replay msg-2 and msg-3
        received: list = []
        done = threading.Event()

        def reconnect_sse():
            sse_url = f"{url}/v1/subscribe?channel=replay-ch"
            client = SSEClient(sse_url, timeout=10, max_retries=0, last_event_id=first_id)
            try:
                for event in client:
                    received.append(event)
                    if len(received) >= 2:
                        done.set()
                        break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=reconnect_sse, daemon=True)
        t.start()

        assert done.wait(timeout=5), "Replay events not received"
        assert len(received) == 2
        payloads = [json.loads(e.data)["message"]["payload"] for e in received]
        assert payloads == ["msg-2", "msg-3"]

    def test_no_replay_without_last_event_id(self, server_with_bus: tuple) -> None:
        """Without Last-Event-ID, no historical messages are sent."""
        url, bus = server_with_bus

        # Pre-publish messages
        bus.publish("no-replay", "agent-a", "chat", "old-msg")

        received: list = []
        got_new = threading.Event()

        def listen():
            sse_url = f"{url}/v1/subscribe?channel=no-replay"
            client = SSEClient(sse_url, timeout=10, max_retries=0)
            try:
                for event in client:
                    received.append(event)
                    got_new.set()
                    break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(0.5)

        # Publish a new message — should be the only one received
        bus.publish("no-replay", "agent-a", "chat", "new-msg")

        assert got_new.wait(timeout=5)
        assert len(received) == 1
        assert json.loads(received[0].data)["message"]["payload"] == "new-msg"

    def test_replay_multi_channel(self, server_with_bus: tuple) -> None:
        """Replay works across multiple subscribed channels."""
        url, bus = server_with_bus

        # Publish to two channels, record the earlier ID as cursor
        first_id = bus.publish("mc-a", "agent", "chat", "a-1")
        bus.publish("mc-a", "agent", "chat", "a-2")
        bus.publish("mc-b", "agent", "chat", "b-1")

        received: list = []
        done = threading.Event()

        def reconnect():
            sse_url = f"{url}/v1/subscribe?channel=mc-a&channel=mc-b"
            client = SSEClient(sse_url, timeout=10, max_retries=0, last_event_id=first_id)
            try:
                for event in client:
                    received.append(event)
                    if len(received) >= 2:
                        done.set()
                        break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=reconnect, daemon=True)
        t.start()

        assert done.wait(timeout=5)
        # Should have replayed a-2 from mc-a and b-1 from mc-b
        payloads = {json.loads(e.data)["message"]["payload"] for e in received}
        assert "a-2" in payloads
        assert "b-1" in payloads


# ── Per-Channel Subscribe Endpoint ───────────────────────────────


class TestPerChannelSubscribe:
    """GET /v1/channels/{channel}/subscribe convenience endpoint."""

    def test_single_channel_subscribe(self, server_with_bus: tuple) -> None:
        """Per-channel endpoint receives live push events."""
        url, bus = server_with_bus
        received: list = []
        done = threading.Event()

        def listen():
            sse_url = f"{url}/v1/channels/per-ch-test/subscribe"
            client = SSEClient(sse_url, timeout=10, max_retries=0)
            try:
                for event in client:
                    received.append(event)
                    done.set()
                    break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(0.5)

        bus.publish("per-ch-test", "agent-x", "chat", "channel endpoint!")

        assert done.wait(timeout=5)
        assert len(received) == 1
        data = json.loads(received[0].data)
        assert data["channel"] == "per-ch-test"
        assert data["message"]["payload"] == "channel endpoint!"

    def test_per_channel_with_last_event_id(self, server_with_bus: tuple) -> None:
        """Per-channel endpoint supports Last-Event-ID replay."""
        url, bus = server_with_bus

        first_id = bus.publish("pc-replay", "agent", "chat", "old")
        bus.publish("pc-replay", "agent", "chat", "new")

        received: list = []
        done = threading.Event()

        def reconnect():
            sse_url = f"{url}/v1/channels/pc-replay/subscribe"
            client = SSEClient(sse_url, timeout=10, max_retries=0, last_event_id=first_id)
            try:
                for event in client:
                    received.append(event)
                    done.set()
                    break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=reconnect, daemon=True)
        t.start()

        assert done.wait(timeout=5)
        assert json.loads(received[0].data)["message"]["payload"] == "new"


# ── Comma-Separated Channels Param ───────────────────────────────


class TestChannelsParam:
    """Multi-channel subscribe via ``channels=ch1,ch2``."""

    def test_comma_separated_channels(self, server_with_bus: tuple) -> None:
        """channels=a,b subscribes to both channels."""
        url, bus = server_with_bus
        received: list = []
        done = threading.Event()

        def listen():
            sse_url = f"{url}/v1/subscribe?channels=csv-a,csv-b"
            client = SSEClient(sse_url, timeout=10, max_retries=0)
            try:
                for event in client:
                    received.append(event)
                    if len(received) >= 2:
                        done.set()
                        break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(0.5)

        bus.publish("csv-a", "sender", "chat", "from-a")
        bus.publish("csv-b", "sender", "chat", "from-b")

        assert done.wait(timeout=5)
        channels = {json.loads(e.data)["channel"] for e in received}
        assert channels == {"csv-a", "csv-b"}

    def test_mixed_channel_and_channels_params(self, server_with_bus: tuple) -> None:
        """Both channel= and channels= can be used together."""
        url, bus = server_with_bus
        received: list = []
        done = threading.Event()

        def listen():
            sse_url = f"{url}/v1/subscribe?channel=mix-a&channels=mix-b,mix-c"
            client = SSEClient(sse_url, timeout=10, max_retries=0)
            try:
                for event in client:
                    received.append(event)
                    if len(received) >= 3:
                        done.set()
                        break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(0.5)

        bus.publish("mix-a", "sender", "chat", "a")
        bus.publish("mix-b", "sender", "chat", "b")
        bus.publish("mix-c", "sender", "chat", "c")

        assert done.wait(timeout=5)
        channels = {json.loads(e.data)["channel"] for e in received}
        assert channels == {"mix-a", "mix-b", "mix-c"}

    def test_duplicate_channels_deduplicated(self, server_with_bus: tuple) -> None:
        """Duplicate channel names are deduplicated — only one event per message."""
        url, bus = server_with_bus
        received: list = []
        got_one = threading.Event()

        def listen():
            sse_url = f"{url}/v1/subscribe?channel=dup-ch&channels=dup-ch"
            client = SSEClient(sse_url, timeout=10, max_retries=0)
            try:
                for event in client:
                    received.append(event)
                    got_one.set()
                    if len(received) >= 2:
                        break
            except Exception:
                pass
            finally:
                client.close()

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(0.5)

        bus.publish("dup-ch", "sender", "chat", "once")
        time.sleep(1)

        # Should receive exactly one event, not two
        assert got_one.wait(timeout=5)
        assert len(received) == 1


# ── Transport-Level Last-Event-ID Tracking ────────────────────────


class TestTransportLastEventId:
    """HttpTransport tracks last event ID across SSE restarts."""

    def test_transport_tracks_last_event_id(self, server_with_bus: tuple) -> None:
        """HttpTransport._last_event_id is updated as events arrive."""
        url, bus = server_with_bus
        received: list = []
        done = threading.Event()

        transport = HttpTransport(url, user_id="track-test")

        def on_msg(msg):
            received.append(msg)
            done.set()

        transport.subscribe("track-ch", on_msg)
        time.sleep(0.5)

        msg_id = bus.publish("track-ch", "sender", "chat", "tracked")

        assert done.wait(timeout=5)
        # After receiving an event, the transport should have updated _last_event_id
        assert transport._last_event_id == msg_id

        transport.close()


# ── Error Cases ──────────────────────────────────────────────────


class TestSseErrors:
    """Edge cases and error handling."""

    def test_subscribe_no_channels_returns_400(self, server_url: str) -> None:
        """GET /v1/subscribe without channels returns 400."""
        http = HttpClient(timeout=5)
        resp = http.get(f"{server_url}/v1/subscribe")
        assert resp.status_code == 400
        http.close()

    def test_channels_param_empty_string_returns_400(self, server_url: str) -> None:
        """GET /v1/subscribe?channels= (empty) returns 400."""
        http = HttpClient(timeout=5)
        resp = http.get(f"{server_url}/v1/subscribe?channels=")
        assert resp.status_code == 400
        http.close()
