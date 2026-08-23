"""Tests for mansio CLI."""

import argparse
import json
import subprocess
import sys
import threading
import time

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.cli import _parse_host_port, _redact_token, parse_args
from mansio.frontends import HttpFrontend

# ── Parse Args ────────────────────────────────────────────────────


class TestParseArgsServe:
    """Tests for serve subcommand argument parsing."""

    def test_defaults_no_subcommand(self):
        """No subcommand defaults to serve."""
        args = parse_args([])
        assert args.command == "serve"
        assert args.db == "mansio.db"
        assert args.admin_port == 8741
        assert args.http is None
        assert args.remote is False
        assert args.token is None
        assert args.no_ui is False
        assert args.log_level == "INFO"

    def test_explicit_serve(self):
        args = parse_args(["serve"])
        assert args.command == "serve"

    def test_serve_custom_db(self):
        args = parse_args(["serve", "-d", "custom.db"])
        assert args.db == "custom.db"

    def test_serve_http_port_only(self):
        args = parse_args(["serve", "--http", "8742"])
        assert args.http == "8742"

    def test_serve_http_host_port(self):
        args = parse_args(["serve", "--http", "0.0.0.0:8742"])
        assert args.http == "0.0.0.0:8742"

    def test_serve_admin_port(self):
        args = parse_args(["serve", "--admin-port", "9000"])
        assert args.admin_port == 9000

    def test_serve_remote(self):
        args = parse_args(["serve", "--remote"])
        assert args.remote is True

    def test_serve_token(self):
        args = parse_args(["serve", "--token", "my-secret"])
        assert args.token == "my-secret"

    def test_serve_no_ui(self):
        args = parse_args(["serve", "--no-ui"])
        assert args.no_ui is True

    def test_serve_log_level(self):
        args = parse_args(["serve", "--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"

    def test_serve_invalid_log_level(self):
        with pytest.raises(SystemExit):
            parse_args(["serve", "--log-level", "TRACE"])

    def test_serve_irc(self):
        args = parse_args(["serve", "--irc", "irc.example.com:6667"])
        assert args.irc == "irc.example.com:6667"

    def test_serve_irc_nick(self):
        args = parse_args(["serve", "--irc", "host:6667", "--irc-nick", "mybot"])
        assert args.irc_nick == "mybot"

    def test_serve_irc_channels(self):
        args = parse_args(["serve", "--irc", "host:6667", "--irc-channels", "tasks", "sync"])
        assert args.irc_channels == ["tasks", "sync"]

    def test_serve_irc_ssl(self):
        args = parse_args(["serve", "--irc", "host:6697", "--irc-ssl"])
        assert args.irc_ssl is True

    def test_serve_irc_defaults(self):
        args = parse_args(["serve"])
        assert args.irc is None
        assert args.irc_nick == "mansio-bot"
        assert args.irc_channels is None
        assert args.irc_ssl is False


class TestParseArgsClient:
    """Tests for client subcommand argument parsing."""

    def test_client_send(self):
        args = parse_args(
            [
                "client",
                "send",
                "-s",
                "http://localhost:8742",
                "-a",
                "my-agent",
                "-c",
                "tasks",
                "hello world",
            ]
        )
        assert args.command == "client"
        assert args.action == "send"
        assert args.server == "http://localhost:8742"
        assert args.agent == "my-agent"
        assert args.channel == "tasks"
        assert args.msg_type == "chat"
        assert args.message == "hello world"

    def test_client_send_custom_type(self):
        args = parse_args(
            [
                "client",
                "send",
                "-s",
                "http://x:8742",
                "-a",
                "aaa",
                "-c",
                "ch",
                "-t",
                "notice",
                "msg",
            ]
        )
        assert args.msg_type == "notice"

    def test_client_poll(self):
        args = parse_args(
            [
                "client",
                "poll",
                "-s",
                "http://localhost:8742",
                "-a",
                "my-agent",
                "-c",
                "tasks",
            ]
        )
        assert args.action == "poll"
        assert args.channel == "tasks"
        assert args.limit == 10
        assert args.follow is False

    def test_client_poll_follow(self):
        args = parse_args(
            [
                "client",
                "poll",
                "-s",
                "http://x:8742",
                "-a",
                "aaa",
                "-c",
                "ch",
                "--follow",
            ]
        )
        assert args.follow is True

    def test_client_poll_limit(self):
        args = parse_args(
            [
                "client",
                "poll",
                "-s",
                "http://x:8742",
                "-a",
                "aaa",
                "-c",
                "ch",
                "-n",
                "50",
            ]
        )
        assert args.limit == 50

    def test_client_channels(self):
        args = parse_args(
            [
                "client",
                "channels",
                "-s",
                "http://localhost:8742",
                "-a",
                "my-agent",
            ]
        )
        assert args.action == "channels"

    def test_client_dm(self):
        args = parse_args(
            [
                "client",
                "dm",
                "-s",
                "http://localhost:8742",
                "-a",
                "alice",
                "--to",
                "bob",
                "hey bob!",
            ]
        )
        assert args.action == "dm"
        assert args.to_agent == "bob"
        assert args.message == "hey bob!"

    def test_client_send_missing_server(self):
        with pytest.raises(SystemExit):
            parse_args(["client", "send", "-a", "aaa", "-c", "ch", "msg"])

    def test_client_send_missing_agent(self):
        with pytest.raises(SystemExit):
            parse_args(["client", "send", "-s", "http://x:8742", "-c", "ch", "msg"])


# ── Helpers ───────────────────────────────────────────────────────


class TestParseHostPort:
    """Tests for _parse_host_port."""

    def test_port_only(self):
        assert _parse_host_port("8742") == ("127.0.0.1", 8742)

    def test_host_and_port(self):
        assert _parse_host_port("0.0.0.0:8742") == ("0.0.0.0", 8742)

    def test_custom_default_host(self):
        assert _parse_host_port("9000", default_host="0.0.0.0") == ("0.0.0.0", 9000)

    def test_invalid_port(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_host_port("abc")

    def test_invalid_host_port(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_host_port("host:abc")


class TestRedactToken:
    """Tests for token redaction."""

    def test_long_token(self):
        token = "sk-abcdef1234567890abcdef1234567890abcdef1234567890"
        result = _redact_token(token)
        assert result.startswith("sk-abcde")
        assert result.endswith("7890")
        assert "..." in result
        assert len(result) < len(token)

    def test_short_token(self):
        assert _redact_token("short") == "short"

    def test_exact_boundary(self):
        token = "a" * 12  # head=8 + tail=4 = 12
        assert _redact_token(token) == token

    def test_one_over_boundary(self):
        token = "a" * 13
        result = _redact_token(token)
        assert result == "aaaaaaaa...aaaa"

    def test_custom_head_tail(self):
        result = _redact_token("abcdefghij", head=3, tail=2)
        assert result == "abc...ij"


# ── CLI Subprocess Tests ──────────────────────────────────────────


class TestCliSubprocess:
    """Test CLI via subprocess."""

    def test_version_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "mansio.cli", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "mansio" in result.stdout

    def test_help_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "mansio.cli", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "serve" in result.stdout
        assert "client" in result.stdout

    def test_serve_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "mansio.cli", "serve", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--http" in result.stdout
        assert "--admin-port" in result.stdout

    def test_client_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "mansio.cli", "client", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "send" in result.stdout
        assert "poll" in result.stdout
        assert "channels" in result.stdout
        assert "dm" in result.stdout


# ── Integration Tests ─────────────────────────────────────────────


@pytest.fixture()
def http_server_url():
    """Start a MansioServer with HttpFrontend on a random port, yield URL."""
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


class TestClientIntegration:
    """Integration tests: subprocess client commands against a live server."""

    def test_send_and_poll(self, http_server_url: str) -> None:
        """Send a message via CLI, then poll it back."""
        # Send
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mansio.cli",
                "client",
                "send",
                "-s",
                http_server_url,
                "-a",
                "test-agent",
                "-c",
                "test-ch",
                "hello from cli",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        msg_id = result.stdout.strip()
        assert msg_id  # should print message ID

        # Poll
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mansio.cli",
                "client",
                "poll",
                "-s",
                http_server_url,
                "-a",
                "test-agent",
                "-c",
                "test-ch",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        messages = [json.loads(line) for line in result.stdout.strip().split("\n")]
        assert len(messages) >= 1
        assert any(m["payload"] == "hello from cli" for m in messages)

    def test_channels(self, http_server_url: str) -> None:
        """List channels after sending a message."""
        # Send to create a channel
        subprocess.run(
            [
                sys.executable,
                "-m",
                "mansio.cli",
                "client",
                "send",
                "-s",
                http_server_url,
                "-a",
                "test-agent",
                "-c",
                "my-channel",
                "seed",
            ],
            capture_output=True,
            text=True,
        )

        # List channels
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mansio.cli",
                "client",
                "channels",
                "-s",
                http_server_url,
                "-a",
                "test-agent",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        channels = result.stdout.strip().split("\n")
        assert "my-channel" in channels

    def test_dm(self, http_server_url: str) -> None:
        """Send a DM via CLI."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mansio.cli",
                "client",
                "dm",
                "-s",
                http_server_url,
                "-a",
                "alice",
                "--to",
                "bob",
                "hey bob!",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        msg_id = result.stdout.strip()
        assert msg_id
