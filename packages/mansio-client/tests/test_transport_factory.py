"""Tests for transport selection by URL scheme."""

from __future__ import annotations

from typing import cast

import pytest
from mansio_client import transport as transport_module
from mansio_client.transport import (
    HttpTransport,
    create_transport,
    register_transport,
)
from mansio_core.transport import Transport


class _FakeTransport:
    def __init__(self, url: str, *, user_id: str, token: str | None) -> None:
        self.url = url
        self.user_id = user_id
        self.token = token

    def close(self) -> None:
        pass


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_http_schemes_select_http_transport(scheme: str) -> None:
    transport = create_transport(f"{scheme}://example.invalid", user_id="agent-x", token=None)
    try:
        assert isinstance(transport, HttpTransport)
    finally:
        transport.close()


def test_unknown_scheme_names_the_scheme() -> None:
    with pytest.raises(ValueError, match="ftp"):
        create_transport("ftp://example.invalid", user_id="agent-x", token=None)


def test_missing_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="no scheme"):
        create_transport("example.invalid", user_id="agent-x", token=None)


def test_registered_factory_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Work on a copy so the registration does not leak into other tests.
    monkeypatch.setattr(
        transport_module, "_TRANSPORT_FACTORIES", dict(transport_module._TRANSPORT_FACTORIES)
    )
    seen: dict[str, object] = {}

    def fake_factory(url: str, *, user_id: str, token: str | None) -> Transport:
        seen.update(url=url, user_id=user_id, token=token)
        return cast(Transport, _FakeTransport(url, user_id=user_id, token=token))

    register_transport("fake", fake_factory)

    transport = create_transport("fake://example.invalid", user_id="agent-x", token="mst-1")

    assert isinstance(transport, _FakeTransport)
    assert seen == {
        "url": "fake://example.invalid",
        "user_id": "agent-x",
        "token": "mst-1",
    }
