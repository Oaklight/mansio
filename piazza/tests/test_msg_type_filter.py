"""Tests for msg_type filter support in query/poll and /v1/query."""

from __future__ import annotations

from piazza import Bus, MemoryBackend
from piazza.backends import SQLiteBackend


class TestMsgTypeFilterMemoryBackend:
    """msg_type filtering via MemoryBackend."""

    def _make_bus(self) -> Bus:
        return Bus(backend=MemoryBackend())

    def test_poll_with_msg_type_returns_matching(self):
        bus = self._make_bus()
        bus.publish("ch", "alice", "chat", "hi")
        bus.publish("ch", "alice", "task", "do stuff")
        bus.publish("ch", "alice", "note", "remember this")

        msgs = bus.poll("ch", msg_type="task")
        assert len(msgs) == 1
        assert msgs[0].msg_type == "task"
        assert msgs[0].payload == "do stuff"

    def test_poll_without_msg_type_returns_all(self):
        bus = self._make_bus()
        bus.publish("ch", "alice", "chat", "hi")
        bus.publish("ch", "alice", "task", "do stuff")
        bus.publish("ch", "alice", "note", "remember this")

        msgs = bus.poll("ch")
        assert len(msgs) == 3

    def test_poll_with_nonexistent_msg_type_returns_empty(self):
        bus = self._make_bus()
        bus.publish("ch", "alice", "chat", "hi")
        bus.publish("ch", "alice", "task", "do stuff")

        msgs = bus.poll("ch", msg_type="nonexistent")
        assert msgs == []

    def test_poll_msg_type_combined_with_after(self):
        bus = self._make_bus()
        id1 = bus.publish("ch", "alice", "task", "first task")
        bus.publish("ch", "alice", "chat", "hello")
        bus.publish("ch", "alice", "task", "second task")

        msgs = bus.poll("ch", after=id1, msg_type="task")
        assert len(msgs) == 1
        assert msgs[0].payload == "second task"


class TestMsgTypeFilterSQLiteBackend:
    """msg_type filtering via SQLiteBackend."""

    def _make_bus(self) -> Bus:
        return Bus(backend=SQLiteBackend(":memory:"))

    def test_poll_with_msg_type_returns_matching(self):
        bus = self._make_bus()
        bus.publish("ch", "alice", "chat", "hi")
        bus.publish("ch", "alice", "task", "do stuff")
        bus.publish("ch", "alice", "note", "remember this")

        msgs = bus.poll("ch", msg_type="task")
        assert len(msgs) == 1
        assert msgs[0].msg_type == "task"
        assert msgs[0].payload == "do stuff"

    def test_poll_without_msg_type_returns_all(self):
        bus = self._make_bus()
        bus.publish("ch", "alice", "chat", "hi")
        bus.publish("ch", "alice", "task", "do stuff")
        bus.publish("ch", "alice", "note", "remember this")

        msgs = bus.poll("ch")
        assert len(msgs) == 3

    def test_poll_with_nonexistent_msg_type_returns_empty(self):
        bus = self._make_bus()
        bus.publish("ch", "alice", "chat", "hi")
        bus.publish("ch", "alice", "task", "do stuff")

        msgs = bus.poll("ch", msg_type="nonexistent")
        assert msgs == []

    def test_poll_msg_type_combined_with_after(self):
        bus = self._make_bus()
        id1 = bus.publish("ch", "alice", "task", "first task")
        bus.publish("ch", "alice", "chat", "hello")
        bus.publish("ch", "alice", "task", "second task")

        msgs = bus.poll("ch", after=id1, msg_type="task")
        assert len(msgs) == 1
        assert msgs[0].payload == "second task"


class TestMsgTypeFilterHTTP:
    """msg_type filter via /v1/query HTTP endpoint."""

    def _publish(self, http, server_url, channel, sender, msg_type, payload):
        resp = http.post(
            f"{server_url}/v1/publish",
            json={"channel": channel, "sender": sender, "msg_type": msg_type, "payload": payload},
        )
        assert resp.status_code == 200, f"publish failed: {resp.status_code} {resp.text}"

    def test_query_with_msg_type(self, server_url):
        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        for msg_type, payload in [
            ("chat", "hi there"),
            ("task", "do stuff"),
            ("note", "remember this"),
        ]:
            self._publish(http, server_url, "testfilter", "alice", msg_type, payload)

        resp = http.get(f"{server_url}/v1/query?channel=testfilter&msg_type=task")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["messages"][0]["msg_type"] == "task"
        assert data["messages"][0]["payload"] == "do stuff"
        http.close()

    def test_query_without_msg_type_returns_all(self, server_url):
        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        for msg_type, payload in [
            ("chat", "hi2"),
            ("task", "do2"),
            ("note", "note2"),
        ]:
            self._publish(http, server_url, "testalltype", "alice", msg_type, payload)

        resp = http.get(f"{server_url}/v1/query?channel=testalltype")
        assert resp.status_code == 200
        assert resp.json()["count"] == 3
        http.close()

    def test_query_msg_type_no_matches(self, server_url):
        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        self._publish(http, server_url, "testnomatch", "alice", "chat", "hello")

        resp = http.get(f"{server_url}/v1/query?channel=testnomatch&msg_type=nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["messages"] == []
        http.close()
