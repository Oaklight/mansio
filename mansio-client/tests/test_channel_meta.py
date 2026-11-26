"""Channel metadata on the client surface (MCP audit follow-up).

``GET /v1/channels/<channel>/meta`` returns owner/visibility/created_at.
The transport parses it into ``ChannelMeta`` and lets a 404 surface as the
API error, because the server uses the same status whether the channel is
missing or unreadable.
"""

from __future__ import annotations

import pytest

from mansio_client import MansioAPIError, MansioClient
from mansio_client.transport import HttpTransport
from mansio_client.types import ChannelMeta


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.urls: list[str] = []

    def get(self, url: str) -> _FakeResponse:
        self.urls.append(url)
        return self.response

    def close(self) -> None:
        pass


def _transport(response: _FakeResponse) -> HttpTransport:
    transport = HttpTransport("http://example.invalid", user_id="test-agent")
    real_http = transport._http
    transport._http = _FakeHttp(response)
    real_http.close()
    return transport


def _payload() -> dict:
    return {
        "channel": {
            "name": "ops",
            "owner": "alice",
            "visibility": "private",
            "created_at": "2026-01-01T00:00:00Z",
        }
    }


class TestTransportChannelMeta:
    def test_parses_channel_meta(self) -> None:
        transport = _transport(_FakeResponse(_payload()))
        try:
            meta = transport.channel_meta("ops")
        finally:
            transport.close()
        assert meta == ChannelMeta(
            name="ops",
            owner="alice",
            visibility="private",
            created_at="2026-01-01T00:00:00Z",
        )

    def test_path_is_quoted(self) -> None:
        transport = _transport(_FakeResponse(_payload()))
        try:
            transport.channel_meta("dm:alice:bob")
        finally:
            transport.close()
        assert transport._http.urls == [
            "http://example.invalid/v1/channels/dm%3Aalice%3Abob/meta"
        ]

    def test_404_surfaces_as_api_error(self) -> None:
        transport = _transport(
            _FakeResponse(
                {"error": "Not Found", "message": "Channel 'ops' not found"},
                status_code=404,
            )
        )
        try:
            with pytest.raises(MansioAPIError) as exc:
                transport.channel_meta("ops")
        finally:
            transport.close()
        assert exc.value.status_code == 404
        assert "not found" in str(exc.value)


class _FakeTransport:
    def __init__(self, meta: ChannelMeta) -> None:
        self.meta = meta
        self.calls: list[str] = []

    def channel_meta(self, channel: str) -> ChannelMeta:
        self.calls.append(channel)
        return self.meta


class TestClientChannelMeta:
    def test_delegates_to_transport(self) -> None:
        expected = ChannelMeta(
            name="ops",
            owner="alice",
            visibility="private",
            created_at="2026-01-01T00:00:00Z",
        )
        transport = _FakeTransport(expected)
        client = MansioClient.__new__(MansioClient)
        client._transport = transport
        try:
            assert client.channel_meta("ops") == expected
        finally:
            del client._transport
        assert transport.calls == ["ops"]
