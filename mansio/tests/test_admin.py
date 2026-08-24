"""Tests for mansio admin panel."""

import json
import urllib.error
import urllib.request

import pytest

from mansio import Bus, MemoryBackend, SQLiteBackend, SQLiteBus

# ============== Storage Extension Tests ==============


class TestBackendExtensions:
    """Test message_count, search, stats, recent_timestamps."""

    @pytest.fixture(params=["sqlite", "memory"])
    def backend(self, request, tmp_path):
        b = SQLiteBackend(tmp_path / "test.db") if request.param == "sqlite" else MemoryBackend()
        yield b
        b.close()

    @pytest.fixture
    def bus_with_data(self, backend):
        bus = Bus(backend=backend)
        bus.publish("chat", "alice", "text", "hello")
        bus.publish("chat", "bob", "text", "hi there")
        bus.publish("chat", "alice", "image", "photo.png")
        bus.publish("sync", "agent-a", "context_sync", '{"key": "value"}')
        bus.publish("sync", "agent-b", "context_sync", '{"key": "value2"}')
        return bus

    def test_message_count_all(self, bus_with_data):
        assert bus_with_data.backend.message_count() == 5

    def test_message_count_channel(self, bus_with_data):
        assert bus_with_data.backend.message_count("chat") == 3
        assert bus_with_data.backend.message_count("sync") == 2

    def test_message_count_empty_channel(self, bus_with_data):
        assert bus_with_data.backend.message_count("nonexistent") == 0

    def test_search_no_filter(self, bus_with_data):
        msgs = bus_with_data.backend.search(limit=10)
        assert len(msgs) == 5

    def test_search_filter_channel(self, bus_with_data):
        msgs = bus_with_data.backend.search(channel="chat", limit=10)
        assert len(msgs) == 3
        assert all(m.channel == "chat" for m in msgs)

    def test_search_filter_sender(self, bus_with_data):
        msgs = bus_with_data.backend.search(sender="alice", limit=10)
        assert len(msgs) == 2
        assert all(m.sender == "alice" for m in msgs)

    def test_search_filter_msg_type(self, bus_with_data):
        msgs = bus_with_data.backend.search(msg_type="context_sync", limit=10)
        assert len(msgs) == 2
        assert all(m.msg_type == "context_sync" for m in msgs)

    def test_search_combined_filters(self, bus_with_data):
        msgs = bus_with_data.backend.search(channel="chat", sender="alice", limit=10)
        assert len(msgs) == 2

    def test_search_with_after(self, bus_with_data):
        all_msgs = bus_with_data.backend.search(limit=10)
        first_id = all_msgs[0].id
        after_msgs = bus_with_data.backend.search(after=first_id, limit=10)
        assert len(after_msgs) == 4

    def test_search_limit(self, bus_with_data):
        msgs = bus_with_data.backend.search(limit=2)
        assert len(msgs) == 2

    def test_stats(self, bus_with_data):
        stats = bus_with_data.backend.stats()
        assert stats["total_messages"] == 5
        assert stats["total_channels"] == 2
        assert stats["total_senders"] == 4  # alice, bob, agent-a, agent-b
        assert len(stats["channel_breakdown"]) == 2
        assert len(stats["msg_type_distribution"]) == 3  # text, image, context_sync

        # Breakdown is sorted by count desc
        assert (
            stats["channel_breakdown"][0]["message_count"]
            >= stats["channel_breakdown"][1]["message_count"]
        )

    def test_stats_empty(self, backend):
        stats = backend.stats()
        assert stats["total_messages"] == 0
        assert stats["total_channels"] == 0

    def test_recent_timestamps(self, bus_with_data):
        timestamps = bus_with_data.backend.recent_timestamps(60)
        assert len(timestamps) == 5
        # Sorted ascending
        assert timestamps == sorted(timestamps)

    def test_recent_timestamps_empty_window(self, bus_with_data):
        # Very short window should still get recent messages (just published)
        timestamps = bus_with_data.backend.recent_timestamps(1)
        assert len(timestamps) == 5


# ============== Auth Tests ==============


class TestSessionAuth:
    def test_auto_generate(self):
        from mansio.admin.auth import SessionAuth

        auth = SessionAuth()
        assert len(auth.password) == 32  # 16 bytes hex

    def test_custom_password(self):
        from mansio.admin.auth import SessionAuth

        auth = SessionAuth("my-secret")
        assert auth.password == "my-secret"

    def test_check_password_correct(self):
        from mansio.admin.auth import SessionAuth

        auth = SessionAuth("test-password")
        assert auth.check_password("test-password") is True

    def test_check_password_wrong(self):
        from mansio.admin.auth import SessionAuth

        auth = SessionAuth("test-password")
        assert auth.check_password("wrong-password") is False

    def test_session_create_validate(self):
        from mansio.admin.auth import SessionAuth

        auth = SessionAuth("test-password")
        token = auth.create_session()
        assert auth.validate_session(token) is True
        assert auth.validate_session("wrong") is False

    def test_sessions_are_unique(self):
        from mansio.admin.auth import SessionAuth

        auth = SessionAuth("test-password")
        t1 = auth.create_session()
        t2 = auth.create_session()
        assert t1 != t2
        assert auth.validate_session(t1) is True
        assert auth.validate_session(t2) is True

    def test_revoke_session(self):
        from mansio.admin.auth import SessionAuth

        auth = SessionAuth("test-password")
        t1 = auth.create_session()
        t2 = auth.create_session()
        auth.revoke_session(t1)
        assert auth.validate_session(t1) is False
        assert auth.validate_session(t2) is True

    def test_backward_compat_alias(self):
        from mansio.admin.auth import TokenAuth

        # TokenAuth is an alias for SessionAuth
        auth = TokenAuth("my-secret")
        assert auth.password == "my-secret"


# ============== Admin Server Tests ==============


class TestAdminServer:
    @pytest.fixture
    def bus(self):
        b = SQLiteBus(":memory:")
        yield b
        b.close()

    def test_start_stop(self, bus):
        from mansio.admin import AdminServer

        server = AdminServer(bus, port=0)
        info = server.start()
        assert server.is_running()
        assert info.url.startswith("http://")
        server.stop()
        assert not server.is_running()

    def test_start_returns_admin_info(self, bus):
        from mansio.admin import AdminServer

        server = AdminServer(bus, port=0)
        info = server.start()
        assert info.host == "127.0.0.1"
        assert info.port > 0
        assert info.password is None
        server.stop()

    def test_double_start_raises(self, bus):
        from mansio.admin import AdminServer

        server = AdminServer(bus, port=0)
        server.start()
        with pytest.raises(RuntimeError, match="already running"):
            server.start()
        server.stop()

    def test_remote_auto_generates_password(self, bus):
        from mansio.admin import AdminServer

        server = AdminServer(bus, remote=True)
        info = server.start()
        assert info.password is not None
        assert len(info.password) == 32
        server.stop()

    def test_admin_constructed_independently(self, bus):
        from mansio.admin import AdminServer

        admin = AdminServer(bus, port=0)
        info = admin.start()
        assert info.url.startswith("http://")
        admin.stop()

    def test_bus_close_does_not_manage_admin(self):
        from mansio.admin import AdminServer

        b = SQLiteBus(":memory:")
        admin = AdminServer(b, port=0)
        admin.start()
        b.close()
        # Bus.close() only closes the backend, not admin
        assert admin.is_running()
        admin.stop()


# ============== API Integration Tests ==============


class TestAdminAPI:
    @pytest.fixture
    def server_url(self):
        from mansio.admin import AdminServer

        bus = SQLiteBus(":memory:")
        bus.publish("chat", "alice", "text", "hello")
        bus.publish("chat", "bob", "text", "hi")
        bus.publish("sync", "agent-a", "context_sync", '{"data": true}')
        admin = AdminServer(bus, port=0)
        info = admin.start()
        yield info.url, bus
        admin.stop()
        bus.close()

    def _get(self, url, path):
        req = urllib.request.Request(url + path)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def _post(self, url, path, data):
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url + path,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def test_root_serves_html(self, server_url):
        url, _ = server_url
        req = urllib.request.Request(url + "/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode()
            assert "Mansio Admin" in content
            assert resp.headers["Content-Type"] == "text/html"

    def test_stats_api(self, server_url):
        url, _ = server_url
        data = self._get(url, "/api/stats")
        assert data["total_messages"] == 3
        assert data["total_channels"] == 2
        assert data["active_subscriptions"] == 0

    def test_get_throughput(self, server_url):
        url, _ = server_url
        data = self._get(url, "/api/stats/throughput")
        assert data["window_seconds"] == 60
        assert len(data["buckets"]) == 60

    def test_get_channels(self, server_url):
        url, _ = server_url
        data = self._get(url, "/api/channels")
        assert len(data["channels"]) == 2
        names = {ch["name"] for ch in data["channels"]}
        assert names == {"chat", "sync"}

    def test_get_channel_detail(self, server_url):
        url, _ = server_url
        data = self._get(url, "/api/channels/chat")
        assert data["name"] == "chat"
        assert data["message_count"] == 2
        assert "alice" in data["senders"]
        assert "bob" in data["senders"]

    def test_get_channel_not_found(self, server_url):
        url, _ = server_url
        try:
            self._get(url, "/api/channels/nonexistent")
            pytest.fail("Should have raised")
        except urllib.error.HTTPError as e:
            assert e.code == 404

    def test_get_messages(self, server_url):
        url, _ = server_url
        data = self._get(url, "/api/messages?channel=chat")
        assert data["count"] == 2
        assert len(data["messages"]) == 2

    def test_get_messages_requires_channel(self, server_url):
        url, _ = server_url
        try:
            self._get(url, "/api/messages")
            pytest.fail("Should have raised")
        except urllib.error.HTTPError as e:
            assert e.code == 400

    def test_get_messages_with_sender_filter(self, server_url):
        url, _ = server_url
        data = self._get(url, "/api/messages?channel=chat&sender=alice")
        assert data["count"] == 1
        assert data["messages"][0]["sender"] == "alice"

    def test_publish_message(self, server_url):
        url, bus = server_url
        result = self._post(
            url,
            "/api/messages",
            {
                "channel": "test",
                "sender": "admin",
                "msg_type": "text",
                "payload": "hello from admin",
            },
        )
        assert result["success"] is True
        assert result["message_id"]

        # Verify message was stored
        msgs = bus.query("test")
        assert len(msgs) == 1
        assert msgs[0].payload == "hello from admin"

    def test_publish_missing_fields(self, server_url):
        url, _ = server_url
        try:
            self._post(url, "/api/messages", {"channel": "test"})
            pytest.fail("Should have raised")
        except urllib.error.HTTPError as e:
            assert e.code == 400

    def test_get_subscriptions_empty(self, server_url):
        url, _ = server_url
        data = self._get(url, "/api/subscriptions")
        assert data["total"] == 0
        assert data["channels"] == []

    def test_get_subscriptions_with_subs(self, server_url):
        url, bus = server_url
        bus.subscribe("chat", lambda m: None)
        data = self._get(url, "/api/subscriptions")
        assert data["total"] == 1
        assert data["channels"][0]["channel"] == "chat"

    def test_cors_headers(self, server_url):
        url, _ = server_url
        req = urllib.request.Request(url + "/api/stats")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.headers["Access-Control-Allow-Origin"] == "*"

    def test_not_found(self, server_url):
        url, _ = server_url
        try:
            self._get(url, "/api/nonexistent")
            pytest.fail("Should have raised")
        except urllib.error.HTTPError as e:
            assert e.code == 404

    def test_auth_required(self):
        from mansio.admin import AdminServer

        bus = SQLiteBus(":memory:")
        admin = AdminServer(bus, port=0, auth_password="secret123")
        info = admin.start()
        try:
            # Without session cookie, API should fail
            try:
                req = urllib.request.Request(info.url + "/api/stats")
                urllib.request.urlopen(req, timeout=5)
                pytest.fail("Should have raised 401")
            except urllib.error.HTTPError as e:
                assert e.code == 401

            # Login to get session cookie
            login_data = json.dumps({"password": "secret123"}).encode()
            login_req = urllib.request.Request(
                info.url + "/api/login",
                data=login_data,
                headers={"Content-Type": "application/json"},
            )
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            with opener.open(login_req, timeout=5) as resp:
                result = json.loads(resp.read())
                assert result["ok"] is True

            # With session cookie should work
            req = urllib.request.Request(info.url + "/api/stats")
            with opener.open(req, timeout=5) as resp:
                data = json.loads(resp.read())
                assert "total_messages" in data

            # Root page (HTML) should be accessible without auth
            req = urllib.request.Request(info.url + "/")
            with urllib.request.urlopen(req, timeout=5) as resp:
                content = resp.read().decode()
                assert "Mansio" in content
        finally:
            admin.stop()
            bus.close()


# ============== Password Redaction Tests ==============


class TestPasswordRedaction:
    """Verify admin password is never logged in plaintext (#45)."""

    def test_password_not_in_start_log(self):
        """AdminServer.start() must log the redacted password, not the raw value."""
        from unittest.mock import patch

        from mansio.admin.server import AdminServer, _redact_password

        password = "super-secret-password-value"
        bus = SQLiteBus(":memory:")
        server = AdminServer(bus, port=0, auth_password=password)

        try:
            with patch("mansio.admin.server.logger") as mock_logger:
                info = server.start()
                assert info.password == password  # password is still returned

                # Verify logger.info was called with redacted password
                mock_logger.info.assert_called_once()
                call_kwargs = mock_logger.info.call_args
                # The raw password must not appear in any argument
                call_str = str(call_kwargs)
                assert password not in call_str, f"Raw password in log call: {call_str}"
                # The redacted version should appear
                assert _redact_password(password) in call_str
        finally:
            server.stop()
            bus.close()

    def test_admin_info_repr_redacts_password(self):
        """AdminInfo.__repr__() must not contain the raw password."""
        from mansio.admin.server import AdminInfo

        password = "another-secret-pw"
        info = AdminInfo(
            host="127.0.0.1", port=8741, url="http://localhost:8741", password=password
        )
        r = repr(info)
        assert password not in r
        assert "****" in r or "*" in r

    def test_redact_password_helper(self):
        """_redact_password masks all but the last 4 chars."""
        from mansio.admin.server import _redact_password

        assert _redact_password("abcdefghij") == "******ghij"
        assert _redact_password("ab") == "****"  # short values fully masked
        assert _redact_password("abcd") == "****"  # exactly tail length
