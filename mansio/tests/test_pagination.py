"""Tests for offset-based pagination (GitHub issue #14).

Verifies that query(offset=N) correctly skips N messages across
all backends and the HTTP frontend, and that the HTTP response
includes total, has_more, and offset fields.
"""

from __future__ import annotations

import urllib.parse

from mansio import Bus, MemoryBackend
from mansio.backends.memory import MemoryBackend as MemoryBackendDirect
from mansio.backends.sqlite import SQLiteBackend

# ── Helpers ───────────────────────────────────────────────────


def _publish_n(bus: Bus, n: int, channel: str = "ch") -> list[str]:
    """Publish n messages and return their IDs."""
    ids = []
    for i in range(n):
        msg_id = bus.publish(channel, "agent", "text", f"msg-{i}")
        ids.append(msg_id)
    return ids


# ── MemoryBackend ─────────────────────────────────────────────


class TestMemoryBackendPagination:
    """Test offset parameter on MemoryBackend.query()."""

    def test_offset_skips_messages(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, offset=2)
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-2", "msg-3", "msg-4"]

    def test_offset_zero_is_default(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, offset=0)
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]

    def test_offset_with_limit_truncates(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=2, offset=3)
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-3", "msg-4"]

    def test_offset_beyond_total_returns_empty(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 3)

        result = backend.query("ch", limit=10, offset=5)
        assert result == []

    def test_offset_with_newest_order(self):
        """offset=2, newest, limit=2: skip 2 most recent, return next 2."""
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 6)

        result = backend.query("ch", limit=2, order="newest", offset=2)
        # 6 msgs: [0,1,2,3,4,5]. Skip 2 newest (4,5), newest 2 of rest = [2,3]
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-2", "msg-3"]

    def test_offset_with_after_cursor(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        ids = _publish_n(bus, 6)

        # After msg-1, offset=2, limit=2 → skip msg-2,msg-3, return msg-4,msg-5
        result = backend.query("ch", after=ids[1], limit=2, offset=2)
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-4", "msg-5"]

    def test_page_through_all_messages(self):
        """Walk through all messages using offset pagination."""
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 7)

        all_payloads = []
        offset = 0
        while True:
            page = backend.query("ch", limit=3, offset=offset)
            if not page:
                break
            all_payloads.extend(m.payload for m in page)
            offset += len(page)

        assert all_payloads == [f"msg-{i}" for i in range(7)]


# ── SQLiteBackend ─────────────────────────────────────────────


class TestSQLiteBackendPagination:
    """Test offset parameter on SQLiteBackend.query()."""

    def test_offset_skips_messages(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, offset=2)
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-2", "msg-3", "msg-4"]
        backend.close()

    def test_offset_zero_is_default(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, offset=0)
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]
        backend.close()

    def test_offset_beyond_total_returns_empty(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 3)

        result = backend.query("ch", limit=10, offset=5)
        assert result == []
        backend.close()

    def test_offset_with_newest_order(self):
        """offset=2, newest, limit=2: skip 2 most recent, return next 2."""
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 6)

        result = backend.query("ch", limit=2, order="newest", offset=2)
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-2", "msg-3"]
        backend.close()

    def test_offset_with_after_cursor(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        ids = _publish_n(bus, 6)

        result = backend.query("ch", after=ids[1], limit=2, offset=2)
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-4", "msg-5"]
        backend.close()

    def test_page_through_all_messages(self):
        """Walk through all messages using offset pagination."""
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 7)

        all_payloads = []
        offset = 0
        while True:
            page = backend.query("ch", limit=3, offset=offset)
            if not page:
                break
            all_payloads.extend(m.payload for m in page)
            offset += len(page)

        assert all_payloads == [f"msg-{i}" for i in range(7)]
        backend.close()


class TestMaildirBackendPagination:
    """Test offset parameter on MaildirBackend.query()."""

    def test_offset_skips_messages(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend

        backend = MaildirBackend(str(tmp_path / "mdir"))
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, offset=2)
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-2", "msg-3", "msg-4"]
        backend.close()

    def test_offset_zero_is_default(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend

        backend = MaildirBackend(str(tmp_path / "mdir"))
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, offset=0)
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]
        backend.close()

    def test_offset_beyond_total_returns_empty(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend

        backend = MaildirBackend(str(tmp_path / "mdir"))
        bus = Bus(backend=backend)
        _publish_n(bus, 3)

        result = backend.query("ch", limit=10, offset=5)
        assert result == []
        backend.close()

    def test_offset_with_newest_order(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend

        backend = MaildirBackend(str(tmp_path / "mdir"))
        bus = Bus(backend=backend)
        _publish_n(bus, 6)

        result = backend.query("ch", limit=2, order="newest", offset=2)
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-2", "msg-3"]
        backend.close()

    def test_offset_with_after_cursor(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend

        backend = MaildirBackend(str(tmp_path / "mdir"))
        bus = Bus(backend=backend)
        ids = _publish_n(bus, 6)

        result = backend.query("ch", after=ids[1], limit=2, offset=2)
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-4", "msg-5"]
        backend.close()

    def test_full_page_walk(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend

        backend = MaildirBackend(str(tmp_path / "mdir"))
        bus = Bus(backend=backend)
        _publish_n(bus, 7)

        collected = []
        offset = 0
        while True:
            page = backend.query("ch", limit=3, offset=offset)
            if not page:
                break
            collected.extend(m.payload for m in page)
            offset += len(page)

        assert collected == [f"msg-{i}" for i in range(7)]
        backend.close()


# ── Bus.query() ───────────────────────────────────────────────


class TestBusQueryPagination:
    """Test offset parameter on Bus.query()."""

    def test_query_offset(self):
        with Bus(backend=MemoryBackend()) as bus:
            _publish_n(bus, 5)
            msgs = bus.query("ch", limit=2, offset=2)
            assert [m.payload for m in msgs] == ["msg-2", "msg-3"]

    def test_query_offset_newest(self):
        with Bus(backend=MemoryBackend()) as bus:
            _publish_n(bus, 6)
            msgs = bus.query("ch", limit=2, order="newest", offset=2)
            assert [m.payload for m in msgs] == ["msg-2", "msg-3"]


# ── HTTP Frontend ─────────────────────────────────────────────


class TestHttpFrontendPagination:
    """Test offset query parameter and response metadata via HTTP."""

    def test_query_with_offset(self, server_url):
        from mansio._vendor.httpclient import Client

        client = Client()
        for i in range(5):
            client.post(
                f"{server_url}/v1/publish",
                json={
                    "channel": "test-pag",
                    "sender": "agent",
                    "msg_type": "text",
                    "payload": f"msg-{i}",
                },
            )

        params = urllib.parse.urlencode({"channel": "test-pag", "limit": "2", "offset": "2"})
        resp = client.get(f"{server_url}/v1/query?{params}")
        data = resp.json()
        client.close()

        assert data["count"] == 2
        assert data["total"] == 5
        assert data["offset"] == 2
        assert data["has_more"] is True
        assert [m["payload"] for m in data["messages"]] == ["msg-2", "msg-3"]

    def test_query_last_page_has_more_false(self, server_url):
        from mansio._vendor.httpclient import Client

        client = Client()
        for i in range(5):
            client.post(
                f"{server_url}/v1/publish",
                json={
                    "channel": "test-pag-last",
                    "sender": "agent",
                    "msg_type": "text",
                    "payload": f"msg-{i}",
                },
            )

        params = urllib.parse.urlencode({"channel": "test-pag-last", "limit": "3", "offset": "3"})
        resp = client.get(f"{server_url}/v1/query?{params}")
        data = resp.json()
        client.close()

        assert data["count"] == 2
        assert data["total"] == 5
        assert data["offset"] == 3
        assert data["has_more"] is False

    def test_query_offset_zero_default(self, server_url):
        from mansio._vendor.httpclient import Client

        client = Client()
        client.post(
            f"{server_url}/v1/publish",
            json={
                "channel": "test-pag-zero",
                "sender": "agent",
                "msg_type": "text",
                "payload": "hello",
            },
        )

        params = urllib.parse.urlencode({"channel": "test-pag-zero"})
        resp = client.get(f"{server_url}/v1/query?{params}")
        data = resp.json()
        client.close()

        assert data["offset"] == 0
        assert "total" in data
        assert "has_more" in data

    def test_query_invalid_offset(self, server_url):
        from mansio._vendor.httpclient import Client

        client = Client()
        params = urllib.parse.urlencode({"channel": "test-pag-inv", "offset": "abc"})
        resp = client.get(f"{server_url}/v1/query?{params}")
        client.close()

        assert resp.status_code == 400

    def test_query_negative_offset(self, server_url):
        from mansio._vendor.httpclient import Client

        client = Client()
        params = urllib.parse.urlencode({"channel": "test-pag-neg", "offset": "-1"})
        resp = client.get(f"{server_url}/v1/query?{params}")
        client.close()

        assert resp.status_code == 400

    def test_transport_offset(self, server_url):
        """HttpTransport.query() passes offset to server."""
        from mansio import HttpTransport

        transport = HttpTransport(server_url)
        for i in range(5):
            transport.publish("test-pag-transport", "agent", "text", f"msg-{i}")

        result = transport.query("test-pag-transport", limit=2, offset=2)
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-2", "msg-3"]
        transport.close()
