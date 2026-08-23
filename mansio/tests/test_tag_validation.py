"""Tests for tag validation in metadata (issue #71).

Verifies server-side validation of metadata.tags:
- Tags must be a list of non-empty strings
- Each tag must be at most 64 characters
- Maximum 20 tags allowed
"""

from __future__ import annotations


class TestTagValidation:
    """Tags in metadata must be validated."""

    def _publish(self, http, url, tags):
        return http.post(
            f"{url}/v1/publish",
            json={
                "channel": "test-tags",
                "sender": "tester",
                "msg_type": "note",
                "payload": "test payload",
                "metadata": {"tags": tags},
            },
        )

    def test_valid_tags_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, ["python", "testing", "mansio"])
        assert resp.status_code == 200
        http.close()

    def test_single_tag_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, ["solo"])
        assert resp.status_code == 200
        http.close()

    def test_empty_list_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, [])
        assert resp.status_code == 200
        http.close()

    def test_non_list_tags_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, "not-a-list")
        assert resp.status_code == 400
        assert "list" in resp.json()["message"].lower()
        http.close()

    def test_dict_tags_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, {"tag": "value"})
        assert resp.status_code == 400
        http.close()

    def test_non_string_tag_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, [42, "valid"])
        assert resp.status_code == 400
        http.close()

    def test_empty_string_tag_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, ["valid", ""])
        assert resp.status_code == 400
        http.close()

    def test_whitespace_only_tag_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, ["valid", "   "])
        assert resp.status_code == 400
        http.close()

    def test_too_many_tags_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        tags = [f"tag-{i}" for i in range(21)]
        resp = self._publish(http, server_url, tags)
        assert resp.status_code == 400
        assert "20" in resp.json()["message"]
        http.close()

    def test_exactly_20_tags_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        tags = [f"tag-{i}" for i in range(20)]
        resp = self._publish(http, server_url, tags)
        assert resp.status_code == 200
        http.close()

    def test_tag_too_long_rejected(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, ["a" * 65])
        assert resp.status_code == 400
        assert "64" in resp.json()["message"]
        http.close()

    def test_tag_exactly_64_chars_accepted(self, server_url: str) -> None:
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = self._publish(http, server_url, ["a" * 64])
        assert resp.status_code == 200
        http.close()

    def test_metadata_without_tags_accepted(self, server_url: str) -> None:
        """Metadata with no tags key should be fine."""
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.post(
            f"{server_url}/v1/publish",
            json={
                "channel": "test-tags",
                "sender": "tester",
                "msg_type": "note",
                "payload": "test",
                "metadata": {"key": "value"},
            },
        )
        assert resp.status_code == 200
        http.close()

    def test_no_metadata_accepted(self, server_url: str) -> None:
        """No metadata at all should be fine."""
        from mansio._vendor.httpclient import Client as HttpClient

        http = HttpClient()
        resp = http.post(
            f"{server_url}/v1/publish",
            json={
                "channel": "test-tags",
                "sender": "tester",
                "msg_type": "note",
                "payload": "test",
            },
        )
        assert resp.status_code == 200
        http.close()
