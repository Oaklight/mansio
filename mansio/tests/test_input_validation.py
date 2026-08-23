"""Tests for input validation in publish and query endpoints.

Covers:
- Malformed/empty JSON body → 400, not 500 (issue #66)
- msg_type validation: empty, whitespace, over-length (issue #67)
- Non-string payload rejection: int, bool, dict, list (issue #73)
- Empty/whitespace payload rejection (issue #51)
- Negative/zero limit rejection (issue #52)
"""

from __future__ import annotations

import pytest

from mansio import MansioClient
from mansio.transport_http import MansioAPIError


class TestMalformedBody:
    """Malformed or empty JSON body should return 400, not 500."""

    def test_empty_body(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.post(
            f"{server_url}/v1/publish",
            data=b"",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400, f"empty body: expected 400, got {resp.status_code}"
        http.close()

    def test_invalid_json(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.post(
            f"{server_url}/v1/publish",
            data=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400, f"invalid json: expected 400, got {resp.status_code}"
        http.close()

    def test_null_body(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.post(
            f"{server_url}/v1/publish",
            data=b"null",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400, f"null body: expected 400, got {resp.status_code}"
        http.close()

    def test_array_body(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.post(
            f"{server_url}/v1/publish",
            json=[1, 2, 3],
        )
        assert resp.status_code == 400, f"array body: expected 400, got {resp.status_code}"
        http.close()


class TestMsgTypeValidation:
    """msg_type must be a non-empty string, max 64 chars."""

    def _publish(self, http, url, msg_type):
        return http.post(
            f"{url}/v1/publish",
            json={
                "channel": "test-msgtype",
                "sender": "tester",
                "msg_type": msg_type,
                "payload": "test payload",
            },
        )

    def test_valid_msg_types(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        for mtype in ["chat", "note", "thought", "custom-type"]:
            resp = self._publish(http, server_url, mtype)
            assert resp.status_code == 200, f"msg_type={mtype!r}: expected 200"
        http.close()

    def test_empty_msg_type_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, "")
        assert resp.status_code == 400
        http.close()

    def test_whitespace_msg_type_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, "   ")
        assert resp.status_code == 400
        http.close()

    def test_overlength_msg_type_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, "a" * 65)
        assert resp.status_code == 400
        http.close()

    def test_64_char_msg_type_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, "a" * 64)
        assert resp.status_code == 200
        http.close()


class TestPayloadTypeValidation:
    """Non-string payloads must be rejected with 400, not 500."""

    def _publish(self, http, url, payload):
        return http.post(
            f"{url}/v1/publish",
            json={
                "channel": "test-paytype",
                "sender": "tester",
                "msg_type": "chat",
                "payload": payload,
            },
        )

    def test_int_payload_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, 42)
        assert resp.status_code == 400, f"int payload: expected 400, got {resp.status_code}"
        http.close()

    def test_bool_payload_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, True)
        assert resp.status_code == 400, f"bool payload: expected 400, got {resp.status_code}"
        http.close()

    def test_dict_payload_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, {"key": "val"})
        assert resp.status_code == 400, f"dict payload: expected 400, got {resp.status_code}"
        http.close()

    def test_list_payload_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, [1, 2, 3])
        assert resp.status_code == 400, f"list payload: expected 400, got {resp.status_code}"
        http.close()

    def test_empty_string_rejected(self, server_url: str) -> None:
        client = MansioClient(server_url, "tester")
        with pytest.raises(MansioAPIError) as exc_info:
            client.channel_send("test-paytype", "")
        assert exc_info.value.status_code == 400
        client.close()

    def test_whitespace_string_rejected(self, server_url: str) -> None:
        client = MansioClient(server_url, "tester")
        with pytest.raises(MansioAPIError) as exc_info:
            client.channel_send("test-paytype", "   \t\n  ")
        assert exc_info.value.status_code == 400
        client.close()

    def test_valid_string_accepted(self, server_url: str) -> None:
        client = MansioClient(server_url, "tester")
        client.channel_send("test-paytype-ok", "hello world")
        msgs = client.channel_read("test-paytype-ok")
        assert len(msgs) == 1
        assert msgs[0].payload == "hello world"
        client.close()


class TestLimitValidation:
    """Limit parameter validation in query endpoint."""

    def test_negative_limit_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.get(f"{server_url}/v1/query?channel=test-lim&limit=-1")
        assert resp.status_code == 400
        http.close()

    def test_zero_limit_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.get(f"{server_url}/v1/query?channel=test-lim&limit=0")
        assert resp.status_code == 400
        http.close()

    def test_positive_limit_accepted(self, server_url: str) -> None:
        client = MansioClient(server_url, "tester")
        client.channel_send("test-lim", "msg1")
        client.channel_send("test-lim", "msg2")
        msgs = client.channel_read("test-lim", limit=1)
        assert len(msgs) == 1
        client.close()

    def test_float_limit_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.get(f"{server_url}/v1/query?channel=test-lim&limit=3.5")
        assert resp.status_code == 400
        http.close()


class TestPayloadSizeLimit:
    """Payload must not exceed 256 KB (issue #58)."""

    def test_large_payload_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        oversized = "a" * (256 * 1024 + 1)  # 256 KB + 1 byte
        resp = http.post(
            f"{server_url}/v1/publish",
            json={
                "channel": "test-size",
                "sender": "tester",
                "msg_type": "text",
                "payload": oversized,
            },
        )
        assert resp.status_code == 400, f"oversized payload: expected 400, got {resp.status_code}"
        http.close()

    def test_max_payload_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        exact = "a" * (256 * 1024)  # exactly 256 KB
        resp = http.post(
            f"{server_url}/v1/publish",
            json={
                "channel": "test-size",
                "sender": "tester",
                "msg_type": "text",
                "payload": exact,
            },
        )
        assert resp.status_code == 200, f"exact 256KB payload: expected 200, got {resp.status_code}"
        http.close()

    def test_normal_payload_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.post(
            f"{server_url}/v1/publish",
            json={
                "channel": "test-size",
                "sender": "tester",
                "msg_type": "text",
                "payload": "hello world",
            },
        )
        assert resp.status_code == 200, f"normal payload: expected 200, got {resp.status_code}"
        http.close()


class TestMetadataValidation:
    """metadata must be a JSON object (dict) or null/absent."""

    def _publish(self, http, url, **extra):
        body = {
            "channel": "test-meta",
            "sender": "tester",
            "msg_type": "chat",
            "payload": "hello",
        }
        body.update(extra)
        return http.post(f"{url}/v1/publish", json=body)

    def test_string_metadata_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, metadata="not-a-dict")
        assert resp.status_code == 400, f"string metadata: expected 400, got {resp.status_code}"
        http.close()

    def test_list_metadata_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, metadata=[1, 2, 3])
        assert resp.status_code == 400, f"list metadata: expected 400, got {resp.status_code}"
        http.close()

    def test_int_metadata_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, metadata=42)
        assert resp.status_code == 400, f"int metadata: expected 400, got {resp.status_code}"
        http.close()

    def test_bool_metadata_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, metadata=True)
        assert resp.status_code == 400, f"bool metadata: expected 400, got {resp.status_code}"
        http.close()

    def test_dict_metadata_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, metadata={"key": "val"})
        assert resp.status_code == 200, f"dict metadata: expected 200, got {resp.status_code}"
        http.close()

    def test_null_metadata_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, metadata=None)
        assert resp.status_code == 200, f"null metadata: expected 200, got {resp.status_code}"
        # Also test omitted metadata
        resp2 = self._publish(http, server_url)
        assert resp2.status_code == 200, f"omitted metadata: expected 200, got {resp2.status_code}"
        http.close()
