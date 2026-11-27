"""The admin UI contract module must match its generator and pass its JS tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GENERATOR = REPO / "scripts" / "gen_admin_contracts.py"
JS_TESTS = [
    Path(__file__).resolve().parent / "js" / "contracts.test.js",
    Path(__file__).resolve().parent / "js" / "router.test.js",
]


def test_contracts_module_is_up_to_date():
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_contracts_and_models_js_tests():
    result = subprocess.run(
        ["node", "--test", *[str(path) for path in JS_TESTS]],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
