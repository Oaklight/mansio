"""Tests for null byte rejection in publish payloads (issue #68).

Verifies that null bytes (\x00) are rejected in:
- payload
- msg_type
- channel
"""

from __future__ import annotations

import pytest


class TestNullByteInPayload:
    """Null bytes in payload must be rejected with 400."""

    def _publish(self, http, url, **overrides):
        body = {
            "channel": "test-null",
            "sender": "tester",
            "msg_type": "chat",
            "payload": "hello",
        }
        body.update(overrides)
        return http.post(f"{url}/v1/publish", json=body)

    def test_null_byte_in_payload_rejected(self, server_url: str) -> None:
        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, payload="hello\x00world")
        assert resp.status_code == 400
        assert "null bytes" in resp.json()["message"].lower()
        http.close()

    def test_only_null_byte_rejected(self, server_url: str) -> None:
        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, payload="\x00")
        assert resp.status_code == 400
        http.close()

    def test_clean_payload_accepted(self, server_url: str) -> None:
        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, payload="normal text")
        assert resp.status_code == 200
        http.close()

    def test_null_byte_in_msg_type_rejected(self, server_url: str) -> None:
        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, msg_type="chat\x00evil")
        assert resp.status_code == 400
        assert "null bytes" in resp.json()["message"].lower()
        http.close()

    def test_null_byte_in_channel_rejected(self, server_url: str) -> None:
        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, channel="test\x00channel")
        assert resp.status_code == 400
        assert "null bytes" in resp.json()["message"].lower()
        http.close()

    def test_unicode_payload_without_null_accepted(self, server_url: str) -> None:
        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, payload="héllo wörld 🌍")
        assert resp.status_code == 200
        http.close()
