"""Tests for metadata size and type validation (issue #69).

Verifies:
- Metadata must be a JSON object (dict), not a string/list/int
- Serialized metadata must not exceed 16KB
- Valid metadata is accepted
- Absent metadata is accepted (optional field)
"""

from __future__ import annotations


class TestMetadataTypeValidation:
    """Metadata must be a dict if provided."""

    def _publish(self, http, url, metadata):
        return http.post(
            f"{url}/v1/publish",
            json={
                "channel": "test-meta",
                "sender": "tester",
                "msg_type": "chat",
                "payload": "test payload",
                "metadata": metadata,
            },
        )

    def test_string_metadata_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, "not a dict")
        assert resp.status_code == 400
        assert "JSON object" in resp.json()["message"]
        http.close()

    def test_list_metadata_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, [1, 2, 3])
        assert resp.status_code == 400
        assert "JSON object" in resp.json()["message"]
        http.close()

    def test_int_metadata_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, 42)
        assert resp.status_code == 400
        http.close()

    def test_bool_metadata_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, True)
        assert resp.status_code == 400
        http.close()

    def test_dict_metadata_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, {"key": "value"})
        assert resp.status_code == 200
        http.close()

    def test_null_metadata_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, None)
        assert resp.status_code == 200
        http.close()

    def test_no_metadata_field_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.post(
            f"{server_url}/v1/publish",
            json={
                "channel": "test-meta",
                "sender": "tester",
                "msg_type": "chat",
                "payload": "test payload",
            },
        )
        assert resp.status_code == 200
        http.close()

    def test_empty_dict_metadata_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, {})
        assert resp.status_code == 200
        http.close()


class TestMetadataSizeLimit:
    """Serialized metadata must not exceed 16KB."""

    def _publish(self, http, url, metadata):
        return http.post(
            f"{url}/v1/publish",
            json={
                "channel": "test-metasize",
                "sender": "tester",
                "msg_type": "chat",
                "payload": "test",
                "metadata": metadata,
            },
        )

    def test_small_metadata_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, {"key": "small value"})
        assert resp.status_code == 200
        http.close()

    def test_oversized_metadata_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        # Create metadata > 16KB
        big_meta = {"data": "x" * 17000}
        resp = self._publish(http, server_url, big_meta)
        assert resp.status_code == 400
        assert "16KB" in resp.json()["message"]
        http.close()

    def test_exactly_16kb_metadata_accepted(self, server_url: str) -> None:
        """Metadata at exactly 16384 bytes should be accepted."""
        import json

        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        # Build metadata that serializes to exactly 16384 bytes
        # '{"data": ""}' = 12 bytes overhead
        overhead = len(json.dumps({"data": ""}, ensure_ascii=False).encode())
        padding = 16384 - overhead
        meta = {"data": "x" * padding}
        assert len(json.dumps(meta, ensure_ascii=False).encode()) == 16384
        resp = self._publish(http, server_url, meta)
        assert resp.status_code == 200
        http.close()

    def test_just_over_16kb_metadata_rejected(self, server_url: str) -> None:
        """Metadata at 16385 bytes should be rejected."""
        import json

        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        overhead = len(json.dumps({"data": ""}, ensure_ascii=False).encode())
        padding = 16384 - overhead + 1
        meta = {"data": "x" * padding}
        assert len(json.dumps(meta, ensure_ascii=False).encode()) == 16385
        resp = self._publish(http, server_url, meta)
        assert resp.status_code == 400
        http.close()
