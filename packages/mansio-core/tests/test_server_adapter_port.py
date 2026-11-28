"""``ServerAdapter`` is a core port (M6e)."""

from __future__ import annotations

from mansio_core.server import ServerAdapter


def test_protocol_is_declared_in_core() -> None:
    assert ServerAdapter.__module__ == "mansio_core.server"
    for member in ("attach", "serve_forever", "shutdown", "address"):
        assert hasattr(ServerAdapter, member), member
