"""Tests for /v1/auth/check and auth enforcement consistency."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class TestAuthCheckEmptyTokenStore:
    """Server with token store but no tokens: API rejects requests."""

    def test_auth_check_reports_required_no_tokens(self, mansio_server):
        url, _ts, _bus, _srv = mansio_server
        with urllib.request.urlopen(f"{url}/v1/auth/check") as resp:
            data = json.loads(resp.read())
        assert data["auth_mode"] == "required"
        assert data["has_tokens"] is False

    def test_channels_returns_403_when_no_tokens(self, mansio_server):
        url, _ts, _bus, _srv = mansio_server
        req = urllib.request.Request(f"{url}/v1/channels")
        try:
            urllib.request.urlopen(req)
            raise AssertionError("Expected 403")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

    def test_auth_check_after_token_created(self, mansio_server):
        url, token_store, _bus, _srv = mansio_server
        token_store.create_token(user_id="agent-1", label="test")
        with urllib.request.urlopen(f"{url}/v1/auth/check") as resp:
            data = json.loads(resp.read())
        assert data["auth_mode"] == "required"
        assert data["has_tokens"] is True


class TestAuthCheckNoAuth:
    """Server without token store (--no-auth): auth disabled."""

    def test_auth_check_reports_disabled(self, server_url):
        with urllib.request.urlopen(f"{server_url}/v1/auth/check") as resp:
            data = json.loads(resp.read())
        assert data["auth_mode"] == "disabled"
        assert data["has_tokens"] is False

    def test_channels_accessible_without_token(self, server_url):
        with urllib.request.urlopen(f"{server_url}/v1/channels") as resp:
            data = json.loads(resp.read())
        assert "channels" in data
