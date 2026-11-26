"""The pre-split ``mansio`` import root still resolves to the new packages."""

from __future__ import annotations

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
