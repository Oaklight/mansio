"""Tests for MansioClient.check_unread() and CLI check command (#267)."""

from __future__ import annotations

import json
import threading
import time

import pytest
from mansio_client import MansioClient
from mansio_client.cli import main as cli_main

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend


@pytest.fixture()
def server_url():
    """Start a MansioServer with HttpFrontend, yield URL."""
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0, allow_unauthenticated=True)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    url = f"http://{host}:{port}"

    yield url

    server.shutdown()


class TestCheckUnread:
    def test_empty_server(self, server_url: str) -> None:
        with MansioClient(server_url, "test-agent") as client:
            assert client.check_unread() == {}

    def test_reports_unread_counts(self, server_url: str) -> None:
        with MansioClient(server_url, "sender-agent") as sender:
            sender.channel_send("general", "hello")
            sender.channel_send("general", "world")
            sender.channel_send("alerts", "beep")

        with MansioClient(server_url, "reader-agent") as reader:
            unread = reader.check_unread()
            assert unread["general"] == 2
            assert unread["alerts"] == 1

    def test_does_not_advance_cursors(self, server_url: str) -> None:
        with MansioClient(server_url, "sender-agent") as sender:
            sender.channel_send("general", "msg1")
            sender.channel_send("general", "msg2")

        with MansioClient(server_url, "reader-agent") as reader:
            unread1 = reader.check_unread()
            assert unread1["general"] == 2

            unread2 = reader.check_unread()
            assert unread2["general"] == 2

            polled = reader.channel_poll("general")
            assert len(polled) == 2

    def test_respects_existing_cursors(self, server_url: str) -> None:
        with MansioClient(server_url, "sender-agent") as sender:
            sender.channel_send("general", "old msg")

        with MansioClient(server_url, "reader-agent") as reader:
            reader.channel_poll("general")

        with MansioClient(server_url, "sender-agent") as sender:
            sender.channel_send("general", "new msg")

        with MansioClient(server_url, "reader-agent") as reader:
            unread = reader.check_unread()
            assert unread["general"] == 1

    def test_excludes_system_channels(self, server_url: str) -> None:
        with MansioClient(server_url, "test-agent") as client:
            client.channel_send("general", "visible")
            unread = client.check_unread()
            assert not any(ch.startswith("_system:") for ch in unread)


class TestCheckCLI:
    def test_exit_1_when_no_unread(self, server_url: str, capsys) -> None:
        with pytest.raises(SystemExit, match="1"):
            cli_main(["-s", server_url, "-a", "test-agent", "check"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total_unread"] == 0
        assert data["unread"] == {}

    def test_exit_0_with_unread(self, server_url: str, capsys) -> None:
        with MansioClient(server_url, "sender-agent") as sender:
            sender.channel_send("general", "hey")

        cli_main(["-s", server_url, "-a", "reader-agent", "check"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total_unread"] == 1
        assert data["unread"]["general"] == 1
