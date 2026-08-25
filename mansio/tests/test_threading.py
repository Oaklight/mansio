"""Tests for message threading (parent_id / thread_id) — issue #87."""

import json
import threading

import pytest

from mansio import Bus, MansioServer, MemoryBackend, Message, SQLiteBackend
from mansio.frontends import HttpFrontend

# ──────────────────────────────────────────────
# Helpers / Fixtures
# ──────────────────────────────────────────────

_CH = "test-threading"


@pytest.fixture()
def bus():
    b = Bus(MemoryBackend())
    yield b
    b.close()


@pytest.fixture()
def sqlite_bus(tmp_path):
    b = Bus(SQLiteBackend(str(tmp_path / "test.db")))
    yield b
    b.close()


@pytest.fixture()
def server_url(bus):
    """Start a MansioServer on a random port and return its URL."""
    frontend = HttpFrontend(host="127.0.0.1", port=0)
    server = MansioServer(bus)
    server.add_frontend(frontend)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    import time
    time.sleep(0.3)
    host, port = frontend.address
    url = f"http://{host}:{port}"
    yield url


# ──────────────────────────────────────────────
# Message dataclass
# ──────────────────────────────────────────────


class TestMessageFields:
    """parent_id and thread_id on the Message dataclass."""

    def test_defaults_none(self):
        m = Message(
            id="1", channel="ch", sender="a",
            msg_type="text", payload="hello", timestamp="t",
        )
        assert m.parent_id is None
        assert m.thread_id is None

    def test_explicit_values(self):
        m = Message(
            id="2", channel="ch", sender="a",
            msg_type="text", payload="hi", timestamp="t",
            parent_id="1", thread_id="1",
        )
        assert m.parent_id == "1"
        assert m.thread_id == "1"


# ──────────────────────────────────────────────
# Bus.publish() threading logic
# ──────────────────────────────────────────────


class TestBusPublishThreading:
    """Bus.publish() with parent_id auto-computes thread_id."""

    def test_publish_without_parent(self, bus):
        mid = bus.publish(_CH, "alice", "text", "root msg")
        msgs = bus.query(_CH)
        assert len(msgs) == 1
        assert msgs[0].parent_id is None
        assert msgs[0].thread_id is None

    def test_reply_to_root(self, bus):
        root_id = bus.publish(_CH, "alice", "text", "root")
        reply_id = bus.publish(_CH, "bob", "text", "reply", parent_id=root_id)
        msgs = bus.query(_CH)
        reply = [m for m in msgs if m.id == reply_id][0]
        assert reply.parent_id == root_id
        assert reply.thread_id == root_id  # root becomes thread_id

    def test_nested_reply_preserves_thread_id(self, bus):
        """Reply to a reply keeps the original root as thread_id."""
        root_id = bus.publish(_CH, "alice", "text", "root")
        reply1_id = bus.publish(_CH, "bob", "text", "r1", parent_id=root_id)
        reply2_id = bus.publish(_CH, "carol", "text", "r2", parent_id=reply1_id)
        msgs = bus.query(_CH)
        r2 = [m for m in msgs if m.id == reply2_id][0]
        assert r2.parent_id == reply1_id
        assert r2.thread_id == root_id  # thread_id traces to root

    def test_deeply_nested(self, bus):
        """Thread 4 levels deep still points to original root."""
        root_id = bus.publish(_CH, "a", "text", "root")
        prev_id = root_id
        for i in range(5):
            prev_id = bus.publish(_CH, "a", "text", f"reply-{i}", parent_id=prev_id)
        msgs = bus.query(_CH)
        last = msgs[-1]
        assert last.thread_id == root_id

    def test_parent_not_found_raises(self, bus):
        with pytest.raises(ValueError, match="not found"):
            bus.publish(_CH, "alice", "text", "orphan", parent_id="nonexistent")

    def test_parent_wrong_channel_raises(self, bus):
        root_id = bus.publish("other-channel", "alice", "text", "root")
        with pytest.raises(ValueError, match="other-channel"):
            bus.publish(_CH, "alice", "text", "cross", parent_id=root_id)


# ──────────────────────────────────────────────
# Bus.query() with thread_id filter
# ──────────────────────────────────────────────


class TestBusQueryThreadFilter:
    """Bus.query() with thread_id filter."""

    def test_query_thread_id(self, bus):
        root_id = bus.publish(_CH, "alice", "text", "root")
        bus.publish(_CH, "bob", "text", "reply1", parent_id=root_id)
        bus.publish(_CH, "carol", "text", "reply2", parent_id=root_id)
        bus.publish(_CH, "dave", "text", "unrelated")  # not in thread

        thread_msgs = bus.query(_CH, thread_id=root_id)
        assert len(thread_msgs) == 2
        assert all(m.thread_id == root_id for m in thread_msgs)

    def test_query_thread_id_empty(self, bus):
        bus.publish(_CH, "alice", "text", "standalone")
        result = bus.query(_CH, thread_id="nonexistent")
        assert result == []

    def test_query_without_thread_returns_all(self, bus):
        root_id = bus.publish(_CH, "alice", "text", "root")
        bus.publish(_CH, "bob", "text", "reply", parent_id=root_id)
        bus.publish(_CH, "carol", "text", "standalone")
        all_msgs = bus.query(_CH)
        assert len(all_msgs) == 3


# ──────────────────────────────────────────────
# Bus.get_message()
# ──────────────────────────────────────────────


class TestBusGetMessage:
    """Bus.get_message() single lookup."""

    def test_found(self, bus):
        mid = bus.publish(_CH, "alice", "text", "hello")
        msg = bus.get_message(mid)
        assert msg is not None
        assert msg.id == mid
        assert msg.payload == "hello"

    def test_not_found(self, bus):
        assert bus.get_message("nonexistent") is None


# ──────────────────────────────────────────────
# MemoryBackend specifics
# ──────────────────────────────────────────────


class TestMemoryBackendThreading:
    """MemoryBackend stores and filters by thread_id."""

    def test_store_and_query(self):
        backend = MemoryBackend()
        m1 = Message(id="1", channel="ch", sender="a", msg_type="t",
                      payload="root", timestamp="t1")
        m2 = Message(id="2", channel="ch", sender="b", msg_type="t",
                      payload="reply", timestamp="t2",
                      parent_id="1", thread_id="1")
        m3 = Message(id="3", channel="ch", sender="c", msg_type="t",
                      payload="other", timestamp="t3")
        backend.store(m1)
        backend.store(m2)
        backend.store(m3)

        result = backend.query("ch", thread_id="1")
        assert len(result) == 1
        assert result[0].id == "2"

    def test_get_message(self):
        backend = MemoryBackend()
        m = Message(id="x", channel="ch", sender="a", msg_type="t",
                    payload="data", timestamp="t")
        backend.store(m)
        assert backend.get_message("x") is not None
        assert backend.get_message("y") is None


# ──────────────────────────────────────────────
# SQLiteBackend specifics
# ──────────────────────────────────────────────


class TestSQLiteBackendThreading:
    """SQLiteBackend stores, migrates, and filters threading fields."""

    def test_store_and_retrieve(self, tmp_path):
        backend = SQLiteBackend(str(tmp_path / "test.db"))
        m = Message(id="1", channel="ch", sender="a", msg_type="t",
                    payload="root", timestamp="t")
        backend.store(m)
        msgs = backend.query("ch")
        assert msgs[0].parent_id is None
        assert msgs[0].thread_id is None
        backend.close()

    def test_thread_fields_roundtrip(self, tmp_path):
        backend = SQLiteBackend(str(tmp_path / "test.db"))
        m = Message(id="2", channel="ch", sender="a", msg_type="t",
                    payload="reply", timestamp="t",
                    parent_id="1", thread_id="1")
        backend.store(m)
        msgs = backend.query("ch")
        assert msgs[0].parent_id == "1"
        assert msgs[0].thread_id == "1"
        backend.close()

    def test_query_thread_filter(self, tmp_path):
        backend = SQLiteBackend(str(tmp_path / "test.db"))
        backend.store(Message(id="1", channel="ch", sender="a", msg_type="t",
                              payload="root", timestamp="t1"))
        backend.store(Message(id="2", channel="ch", sender="b", msg_type="t",
                              payload="r1", timestamp="t2",
                              parent_id="1", thread_id="1"))
        backend.store(Message(id="3", channel="ch", sender="c", msg_type="t",
                              payload="other", timestamp="t3"))
        result = backend.query("ch", thread_id="1")
        assert len(result) == 1
        assert result[0].id == "2"
        backend.close()

    def test_get_message(self, tmp_path):
        backend = SQLiteBackend(str(tmp_path / "test.db"))
        backend.store(Message(id="x", channel="ch", sender="a", msg_type="t",
                              payload="data", timestamp="t"))
        assert backend.get_message("x") is not None
        assert backend.get_message("y") is None
        backend.close()

    def test_idempotent_migration(self, tmp_path):
        """Opening the DB twice doesn't fail on duplicate columns."""
        db_path = str(tmp_path / "migrate.db")
        b1 = SQLiteBackend(db_path)
        b1.close()
        b2 = SQLiteBackend(db_path)  # should not raise
        b2.close()


# ──────────────────────────────────────────────
# HTTP API integration
# ──────────────────────────────────────────────


class TestHTTPThreading:
    """End-to-end threading via HTTP API."""

    def _publish(self, url, channel, sender, payload, parent_id=None):
        import urllib.request
        body: dict = {
            "channel": channel,
            "sender": sender,
            "msg_type": "text",
            "payload": payload,
        }
        if parent_id:
            body["parent_id"] = parent_id
        req = urllib.request.Request(
            f"{url}/v1/publish",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def _query(self, url, channel, thread_id=None):
        import urllib.request, urllib.parse
        params = {"channel": channel}
        if thread_id:
            params["thread_id"] = thread_id
        qurl = f"{url}/v1/query?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(qurl) as resp:
            return json.loads(resp.read())

    def test_publish_with_parent_id(self, server_url):
        root = self._publish(server_url, _CH, "alice", "root")
        root_id = root["message_id"]
        reply = self._publish(server_url, _CH, "bob", "reply", parent_id=root_id)
        reply_id = reply["message_id"]

        data = self._query(server_url, _CH)
        msgs = data["messages"]
        reply_msg = [m for m in msgs if m["id"] == reply_id][0]
        assert reply_msg["parent_id"] == root_id
        assert reply_msg["thread_id"] == root_id

    def test_query_thread_filter(self, server_url):
        root = self._publish(server_url, _CH, "alice", "root")
        root_id = root["message_id"]
        self._publish(server_url, _CH, "bob", "reply1", parent_id=root_id)
        self._publish(server_url, _CH, "carol", "standalone")

        data = self._query(server_url, _CH, thread_id=root_id)
        assert len(data["messages"]) == 1
        assert data["messages"][0]["thread_id"] == root_id

    def test_parent_not_found_404(self, server_url):
        import urllib.request, urllib.error
        body = json.dumps({
            "channel": _CH,
            "sender": "alice",
            "msg_type": "text",
            "payload": "orphan",
            "parent_id": "nonexistent",
        }).encode()
        req = urllib.request.Request(
            f"{server_url}/v1/publish",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 404

    def test_parent_wrong_channel_400(self, server_url):
        import urllib.request, urllib.error
        root = self._publish(server_url, "other-ch", "alice", "root")
        root_id = root["message_id"]
        body = json.dumps({
            "channel": _CH,
            "sender": "alice",
            "msg_type": "text",
            "payload": "cross",
            "parent_id": root_id,
        }).encode()
        req = urllib.request.Request(
            f"{server_url}/v1/publish",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400

    def test_msg_to_dict_omits_none(self, server_url):
        """Root messages don't have parent_id/thread_id keys in response."""
        self._publish(server_url, _CH, "alice", "root")
        data = self._query(server_url, _CH)
        root_msg = data["messages"][0]
        assert "parent_id" not in root_msg
        assert "thread_id" not in root_msg


# ──────────────────────────────────────────────
# HttpTransport threading
# ──────────────────────────────────────────────


class TestTransportThreading:
    """HttpTransport.publish() and query() with threading params."""

    def test_publish_parent_id(self, server_url):
        from mansio.transport_http import HttpTransport
        t = HttpTransport(server_url)
        root_id = t.publish(_CH, "alice", "text", "root")
        reply_id = t.publish(_CH, "bob", "text", "reply", parent_id=root_id)
        msgs = t.query(_CH)
        reply = [m for m in msgs if m.id == reply_id][0]
        assert reply.parent_id == root_id
        assert reply.thread_id == root_id

    def test_query_thread_filter(self, server_url):
        from mansio.transport_http import HttpTransport
        t = HttpTransport(server_url)
        root_id = t.publish(_CH, "alice", "text", "root")
        t.publish(_CH, "bob", "text", "reply", parent_id=root_id)
        t.publish(_CH, "carol", "text", "standalone")

        thread = t.query(_CH, thread_id=root_id)
        assert len(thread) == 1
        assert thread[0].thread_id == root_id

        all_msgs = t.query(_CH)
        assert len(all_msgs) == 3


# ──────────────────────────────────────────────
# MaildirBackend threading
# ──────────────────────────────────────────────


class TestMaildirBackendThreading:
    """MaildirBackend serializes threading fields in email headers."""

    def test_roundtrip(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend
        backend = MaildirBackend(str(tmp_path / "md"))
        m = Message(id="1", channel="ch", sender="a", msg_type="t",
                    payload="reply", timestamp="2026-01-01T00:00:00Z",
                    parent_id="root", thread_id="root")
        backend.store(m)
        msgs = backend.query("ch")
        assert len(msgs) == 1
        assert msgs[0].parent_id == "root"
        assert msgs[0].thread_id == "root"
        backend.close()

    def test_no_thread_fields(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend
        backend = MaildirBackend(str(tmp_path / "md"))
        m = Message(id="2", channel="ch", sender="a", msg_type="t",
                    payload="root", timestamp="2026-01-01T00:00:00Z")
        backend.store(m)
        msgs = backend.query("ch")
        assert msgs[0].parent_id is None
        assert msgs[0].thread_id is None
        backend.close()

    def test_query_thread_filter(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend
        backend = MaildirBackend(str(tmp_path / "md"))
        backend.store(Message(id="1", channel="ch", sender="a", msg_type="t",
                              payload="root", timestamp="t1"))
        backend.store(Message(id="2", channel="ch", sender="b", msg_type="t",
                              payload="r1", timestamp="t2",
                              parent_id="1", thread_id="1"))
        backend.store(Message(id="3", channel="ch", sender="c", msg_type="t",
                              payload="other", timestamp="t3"))
        result = backend.query("ch", thread_id="1")
        assert len(result) == 1
        assert result[0].id == "2"
        backend.close()

    def test_get_message(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend
        backend = MaildirBackend(str(tmp_path / "md"))
        backend.store(Message(id="x", channel="ch", sender="a", msg_type="t",
                              payload="data", timestamp="t"))
        assert backend.get_message("x") is not None
        assert backend.get_message("y") is None
        backend.close()
