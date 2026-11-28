"""The web package imports ``ServerAdapter`` from core (M6e)."""

from __future__ import annotations

import importlib

import pytest
from mansio_core.server import ServerAdapter


def test_web_exports_the_core_protocol() -> None:
    from mansio_web import ServerAdapter as WebServerAdapter

    assert WebServerAdapter is ServerAdapter

    from mansio_web.adapters import ServerAdapter as AdaptersServerAdapter

    assert AdaptersServerAdapter is ServerAdapter


def test_legacy_base_module_is_not_importable() -> None:
    with pytest.raises(ModuleNotFoundError, match="mansio_web.adapters.base"):
        importlib.import_module("mansio_web.adapters.base")
