"""Tests for intent field (GitHub issue #102, Phase 1).

Verifies that the ``intent`` field round-trips through publish/query
across all backends and the HTTP frontend, and supports query filtering.
"""

from __future__ import annotations

import urllib.parse

from mansio import Bus, MemoryBackend
from mansio.backends.memory import MemoryBackend as MemoryBackendDirect
from mansio.backends.sqlite import SQLiteBackend

# ── MemoryBackend ─────────────────────────────────────────────


class TestMemoryBackendIntent:
    """Test intent field on MemoryBackend."""

    def test_intent_round_trips(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        bus.publish("ch", "agent", "text", "hello", intent="FYI_ONLY")

        msgs = backend.query("ch")
        assert len(msgs) == 1
        assert msgs[0].intent == "FYI_ONLY"

    def test_intent_none_by_default(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        bus.publish("ch", "agent", "text", "hello")

        msgs = backend.query("ch")
        assert msgs[0].intent is None

    def test_query_filter_by_intent(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        bus.publish("ch", "a", "text", "fyi", intent="FYI_ONLY")
        bus.publish("ch", "b", "text", "question", intent="DIRECT_QUESTION")
        bus.publish("ch", "c", "text", "plain")

        result = backend.query("ch", intent="DIRECT_QUESTION")
        assert len(result) == 1
        assert result[0].payload == "question"

    def test_query_filter_intent_no_match(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        bus.publish("ch", "a", "text", "hello", intent="FYI_ONLY")

        result = backend.query("ch", intent="REQUIRES_RESPONSE")
        assert result == []

    def test_multiple_intents_coexist(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        bus.publish("ch", "a", "text", "msg1", intent="FYI_ONLY")
        bus.publish("ch", "b", "text", "msg2", intent="REQUIRES_RESPONSE")
        bus.publish("ch", "c", "text", "msg3", intent="PASS_FLOOR")
        bus.publish("ch", "d", "text", "msg4")  # no intent

        all_msgs = backend.query("ch")
        assert len(all_msgs) == 4

        fyi = backend.query("ch", intent="FYI_ONLY")
        assert len(fyi) == 1
        assert fyi[0].payload == "msg1"

        resp = backend.query("ch", intent="REQUIRES_RESPONSE")
        assert len(resp) == 1
        assert resp[0].payload == "msg2"

    def test_intent_with_thread_filter(self):
        """Intent and thread_id filters compose correctly."""
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        root_id = bus.publish("ch", "a", "text", "root", intent="DIRECT_QUESTION")
        bus.publish("ch", "b", "text", "reply", intent="FYI_ONLY", parent_id=root_id)
        bus.publish("ch", "c", "text", "other", intent="FYI_ONLY")

        # Filter by thread + intent
        result = backend.query("ch", thread_id=root_id, intent="FYI_ONLY")
        assert len(result) == 1
        assert result[0].payload == "reply"


# ── SQLiteBackend ─────────────────────────────────────────────


class TestSQLiteBackendIntent:
    """Test intent field on SQLiteBackend."""

    def test_intent_round_trips(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        bus.publish("ch", "agent", "text", "hello", intent="REQUIRES_RESPONSE")

        msgs = backend.query("ch")
        assert len(msgs) == 1
        assert msgs[0].intent == "REQUIRES_RESPONSE"
        backend.close()

    def test_intent_none_by_default(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        bus.publish("ch", "agent", "text", "hello")

        msgs = backend.query("ch")
        assert msgs[0].intent is None
        backend.close()

    def test_query_filter_by_intent(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        bus.publish("ch", "a", "text", "fyi", intent="FYI_ONLY")
        bus.publish("ch", "b", "text", "question", intent="DIRECT_QUESTION")
        bus.publish("ch", "c", "text", "plain")

        result = backend.query("ch", intent="DIRECT_QUESTION")
        assert len(result) == 1
        assert result[0].payload == "question"
        backend.close()

    def test_query_filter_intent_with_offset(self):
        """Intent filter composes with offset pagination."""
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        for i in range(5):
            bus.publish("ch", "a", "text", f"fyi-{i}", intent="FYI_ONLY")
        bus.publish("ch", "b", "text", "noise")

        result = backend.query("ch", intent="FYI_ONLY", limit=2, offset=2)
        assert len(result) == 2
        assert [m.payload for m in result] == ["fyi-2", "fyi-3"]
        backend.close()

    def test_intent_survives_get_message(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        msg_id = bus.publish("ch", "a", "text", "hello", intent="PASS_FLOOR")

        msg = backend.get_message(msg_id)
        assert msg is not None
        assert msg.intent == "PASS_FLOOR"
        backend.close()


# ── Bus.query() ───────────────────────────────────────────────


class TestBusQueryIntent:
    """Test intent parameter on Bus.query()."""

    def test_query_filter_intent(self):
        with Bus(backend=MemoryBackend()) as bus:
            bus.publish("ch", "a", "text", "fyi", intent="FYI_ONLY")
            bus.publish("ch", "b", "text", "q", intent="DIRECT_QUESTION")

            msgs = bus.query("ch", intent="FYI_ONLY")
            assert len(msgs) == 1
            assert msgs[0].payload == "fyi"

    def test_publish_intent_round_trip(self):
        with Bus(backend=MemoryBackend()) as bus:
            msg_id = bus.publish("ch", "a", "text", "hello", intent="PASS_FLOOR")
            msg = bus.get_message(msg_id)
            assert msg is not None
            assert msg.intent == "PASS_FLOOR"


# ── HTTP Frontend ─────────────────────────────────────────────


class TestHttpFrontendIntent:
    """Test intent via HTTP publish and query endpoints."""

    def test_publish_with_intent(self, server_url):
        from mansio._vendor.httpclient import Client

        client = Client()
        resp = client.post(
            f"{server_url}/v1/publish",
            json={
                "channel": "test-intent",
                "sender": "agent",
                "msg_type": "text",
                "payload": "hello",
                "intent": "REQUIRES_RESPONSE",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        msg_id = data["message_id"]

        # Query back
        params = urllib.parse.urlencode({"channel": "test-intent"})
        resp = client.get(f"{server_url}/v1/query?{params}")
        data = resp.json()
        client.close()

        assert data["count"] == 1
        assert data["messages"][0]["intent"] == "REQUIRES_RESPONSE"
        assert data["messages"][0]["id"] == msg_id

    def test_query_filter_by_intent(self, server_url):
        from mansio._vendor.httpclient import Client

        client = Client()
        for intent in ("FYI_ONLY", "DIRECT_QUESTION", "FYI_ONLY"):
            client.post(
                f"{server_url}/v1/publish",
                json={
                    "channel": "test-intent-filter",
                    "sender": "agent",
                    "msg_type": "text",
                    "payload": f"msg-{intent}",
                    "intent": intent,
                },
            )

        params = urllib.parse.urlencode(
            {"channel": "test-intent-filter", "intent": "DIRECT_QUESTION"}
        )
        resp = client.get(f"{server_url}/v1/query?{params}")
        data = resp.json()
        client.close()

        assert data["count"] == 1
        assert data["messages"][0]["intent"] == "DIRECT_QUESTION"

    def test_publish_without_intent_returns_null(self, server_url):
        from mansio._vendor.httpclient import Client

        client = Client()
        client.post(
            f"{server_url}/v1/publish",
            json={
                "channel": "test-intent-none",
                "sender": "agent",
                "msg_type": "text",
                "payload": "plain",
            },
        )

        params = urllib.parse.urlencode({"channel": "test-intent-none"})
        resp = client.get(f"{server_url}/v1/query?{params}")
        data = resp.json()
        client.close()

        assert data["count"] == 1
        # intent omitted from response when None (sparse serialization)
        assert "intent" not in data["messages"][0]

    def test_transport_intent(self, server_url):
        """HttpTransport passes intent through publish and query."""
        from mansio import HttpTransport

        transport = HttpTransport(server_url)
        transport.publish("test-intent-transport", "agent", "text", "hello", intent="PASS_FLOOR")

        result = transport.query("test-intent-transport")
        assert len(result) == 1
        assert result[0].intent == "PASS_FLOOR"

        # Filter
        empty = transport.query("test-intent-transport", intent="FYI_ONLY")
        assert empty == []
        transport.close()
