"""The removed ``mansio_client.types`` re-export does not resolve."""

from __future__ import annotations

import importlib

import pytest


def test_legacy_types_module_is_not_importable() -> None:
    with pytest.raises(ModuleNotFoundError, match="mansio_client.types"):
        importlib.import_module("mansio_client.types")
