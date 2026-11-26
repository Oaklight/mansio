"""Tests for HttpTransport.require_auth (issue #316).

The server reports ``auth_mode`` as ``"required"`` or ``"disabled"``; a
server that requires auth but has no tokens yet still reports ``"required"``.
The property previously read a ``require_auth`` key the endpoint never sent.
"""

from __future__ import annotations

from mansio_client.transport import HttpTransport


class _FakeResponse:
    status_code = 200
    content = b"{}"

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def get(self, url: str) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(self.payload)

    def close(self) -> None:
        pass


def _transport(payload: dict) -> HttpTransport:
    transport = HttpTransport("http://example.invalid", user_id="test-agent")
    real_http = transport._http
    transport._http = _FakeHttp(payload)
    real_http.close()
    return transport


class TestRequireAuth:
    def test_required(self) -> None:
        transport = _transport({"auth_mode": "required", "has_tokens": True})
        try:
            assert transport.require_auth is True
        finally:
            transport.close()

    def test_required_without_tokens(self) -> None:
        """Auth required with no tokens yet is a distinct state; still True."""
        transport = _transport({"auth_mode": "required", "has_tokens": False})
        try:
            assert transport.require_auth is True
        finally:
            transport.close()

    def test_disabled(self) -> None:
        transport = _transport({"auth_mode": "disabled", "has_tokens": False})
        try:
            assert transport.require_auth is False
        finally:
            transport.close()

    def test_cached_after_first_access(self) -> None:
        transport = _transport({"auth_mode": "required", "has_tokens": True})
        try:
            assert transport.require_auth is True
            assert transport.require_auth is True
            assert transport._http.calls == 1
        finally:
            transport.close()
