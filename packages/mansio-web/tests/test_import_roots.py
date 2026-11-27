"""The pre-split import roots are gone, and the remaining hints stay clear."""

from __future__ import annotations

import importlib

import mansio_web
import pytest


def test_legacy_mansio_package_is_not_importable() -> None:
    with pytest.raises(ModuleNotFoundError, match="mansio"):
        importlib.import_module("mansio")


def test_web_package_points_removed_transport_names_at_client() -> None:
    for name in ("HttpTransport", "MansioAPIError"):
        with pytest.raises(ImportError, match="mansio-client"):
            getattr(mansio_web, name)
