"""The pre-split ``mansio`` import root still resolves to the new packages."""

from __future__ import annotations

import importlib
import subprocess
import sys

import mansio
import mansio_core
import mansio_web


def test_top_level_names_are_the_same_objects() -> None:
    assert mansio.Bus is mansio_core.Bus
    assert mansio.MemoryBackend is mansio_core.MemoryBackend
    assert mansio.MansioServer is mansio_web.MansioServer
    assert mansio.HttpTransport is mansio_web.HttpTransport
    assert mansio.__version__ == mansio_web.__version__


def test_documented_submodules_still_import() -> None:
    from mansio import admin, cli, frontends
    from mansio.admin.metrics import MetricsCollector
    from mansio.bus import Bus
    from mansio.frontends.http import HttpFrontend
    from mansio.types import Message

    assert Bus is mansio_core.Bus
    assert Message is mansio_core.Message
    assert HttpFrontend is mansio_web.frontends.http.HttpFrontend
    assert MetricsCollector is mansio_core.metrics.MetricsCollector
    assert frontends.HttpFrontend is HttpFrontend
    assert admin.AdminServer is mansio_web.admin.AdminServer
    assert cli.main is mansio_web.cli.main


def test_python_m_mansio_cli_still_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mansio.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "serve" in result.stdout


def test_alias_import_does_not_mutate_target_spec() -> None:
    """Importing a routed alias must leave the target's __spec__ untouched."""
    for alias, target in mansio._ROUTES.items():
        importlib.import_module(alias)
        target_module = importlib.import_module(target)
        assert target_module.__spec__ is not None
        assert target_module.__spec__.name == target, alias
        alias_module = sys.modules[alias]
        assert alias_module is not target_module
        assert alias_module.__name__ == alias


def test_bare_import_exposes_submodule_attributes() -> None:
    """The pre-split package exposed submodules after a bare import."""
    code = (
        "import mansio, mansio_core, mansio_web; "
        "assert mansio.types.Message is mansio_core.Message; "
        "assert mansio.bus.Bus is mansio_core.Bus; "
        "assert mansio.frontends.HttpFrontend is "
        "mansio_web.frontends.HttpFrontend; "
        "print('attributes OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "attributes OK" in result.stdout


def test_target_reload_works_after_alias_import() -> None:
    """Reloading a canonical module after aliasing it must use its real name."""
    code = (
        "import importlib, mansio.types, mansio_core.types as target; "
        "assert target.__spec__.name == 'mansio_core.types'; "
        "importlib.reload(target); "
        "assert target.__spec__.name == 'mansio_core.types'; "
        "print('reload OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "reload OK" in result.stdout
