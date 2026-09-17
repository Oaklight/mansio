"""Integration tests for examples/adapters/poll-mansio.sh."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "examples" / "adapters" / "poll-mansio.sh"


@pytest.fixture()
def http_server_url():
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    host, port = frontend.address
    url = f"http://{host}:{port}"

    yield url

    server.shutdown()
    bus.close()


class TestPollScript:
    def test_no_messages_exits_zero(self, http_server_url: str) -> None:
        """Script exits 0 with 'No new messages' when there's nothing to poll."""
        result = subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "MANSIO_URL": http_server_url,
                "MANSIO_USER_ID": "test-agent",
            },
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        output = json.loads(result.stdout.strip())
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "No channels found" in ctx or "No new messages" in ctx

    def test_polls_messages(self, http_server_url: str) -> None:
        """Script outputs messages sent to a channel."""
        # Send a message via mansio-client CLI
        send = subprocess.run(
            [
                sys.executable,
                "-m",
                "mansio_client.cli",
                "--server",
                http_server_url,
                "--agent",
                "sender-agent",
                "send",
                "-c",
                "general",
                "hello from test",
            ],
            capture_output=True,
            text=True,
        )
        assert send.returncode == 0, f"send failed: {send.stderr}"

        result = subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "MANSIO_URL": http_server_url,
                "MANSIO_USER_ID": "poll-agent",
            },
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        output = json.loads(result.stdout.strip())
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "hello from test" in ctx
        assert "sender-agent" in ctx

    def test_specific_channels(self, http_server_url: str) -> None:
        """MANSIO_CHANNELS limits which channels are polled."""
        # Send to two channels
        for ch in ("alpha", "beta"):
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mansio_client.cli",
                    "--server",
                    http_server_url,
                    "--agent",
                    "sender",
                    "send",
                    "-c",
                    ch,
                    f"msg in {ch}",
                ],
                capture_output=True,
                text=True,
            )

        result = subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "MANSIO_URL": http_server_url,
                "MANSIO_USER_ID": "poll-agent",
                "MANSIO_CHANNELS": "alpha",
            },
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        output = json.loads(result.stdout.strip())
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "msg in alpha" in ctx
        assert "msg in beta" not in ctx

    def test_bad_server_url_fails(self) -> None:
        """Script fails visibly when the server is unreachable."""
        result = subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "MANSIO_URL": "http://127.0.0.1:1",
                "MANSIO_USER_ID": "test-agent",
            },
        )
        assert result.returncode != 0
