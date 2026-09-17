"""Tests for mansio CLI."""

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.cli import (
    _check_network_exposure,
    _create_token_store,
    _parse_host_port,
    _redact_token,
    _resolve_admin_auth,
    parse_args,
)
from mansio.frontends import HttpFrontend

# ── Parse Args ────────────────────────────────────────────────────


class TestParseArgsServe:
    """Tests for serve subcommand argument parsing."""

    def test_defaults_no_subcommand(self):
        """No subcommand defaults to serve."""
        args = parse_args([])
        assert args.command == "serve"
        assert args.db is None  # default handled in _create_bus
        assert args.admin_port == 8741
        assert args.http is None
        assert args.remote is False
        assert args.token is None
        assert args.admin_password is None
        assert args.host is None
        assert args.no_ui is False
        assert args.log_level == "INFO"
        assert args.nats is None

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

    def test_serve_nats(self):
        args = parse_args(["serve", "--nats", "nats://localhost:4222"])
        assert args.nats == "nats://localhost:4222"
        assert args.db is None
        assert args.maildir is None

    def test_serve_nats_mutually_exclusive_with_db(self):
        with pytest.raises(SystemExit):
            parse_args(["serve", "--nats", "nats://localhost:4222", "-d", "test.db"])

    def test_serve_nats_mutually_exclusive_with_maildir(self):
        with pytest.raises(SystemExit):
            parse_args(["serve", "--nats", "nats://localhost:4222", "--maildir", "/tmp/mail"])

    def test_serve_host(self):
        args = parse_args(["serve", "--host", "192.168.1.100"])
        assert args.host == "192.168.1.100"

    def test_serve_admin_password(self):
        args = parse_args(["serve", "--admin-password", "my-secret"])
        assert args.admin_password == "my-secret"

    def test_serve_admin_password_and_token_both_set(self):
        """Both --admin-password and --token can be parsed (conflict checked at runtime)."""
        args = parse_args(["serve", "--admin-password", "pw1", "--token", "pw2"])
        assert args.admin_password == "pw1"
        assert args.token == "pw2"

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
                "-u",
                "my-agent",
                "-c",
                "tasks",
                "hello world",
            ]
        )
        assert args.command == "client"
        assert args.action == "send"
        assert args.server == "http://localhost:8742"
        assert args.user == "my-agent"
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
                "-u",
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
                "-u",
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
                "-u",
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
                "-u",
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
                "-u",
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
                "-u",
                "alice",
                "--to",
                "bob",
                "hey bob!",
            ]
        )
        assert args.action == "dm"
        assert args.to_user == "bob"
        assert args.message == "hey bob!"

    def test_client_send_missing_server(self):
        with pytest.raises(SystemExit):
            parse_args(["client", "send", "-u", "aaa", "-c", "ch", "msg"])

    def test_client_send_missing_user(self):
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
        assert "--nats" in result.stdout
        assert "--host" in result.stdout
        assert "--admin-password" in result.stdout

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
                "-u",
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
                "-u",
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
                "-u",
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
                "-u",
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
                "-u",
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


# ── Serve Startup and Network Exposure Guard ──────────────────────


class _RecordingLogger:
    """Structlog-shaped logger that records calls instead of emitting them.

    Only accepts ``(event, **kwargs)``, matching the vendored ``BoundLogger``
    signature, so a printf-style call raises here exactly as it does at runtime.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def _record(self, level: str, event: str, **kwargs) -> None:
        self.calls.append((level, event, kwargs))

    def debug(self, event: str, **kwargs) -> None:
        self._record("debug", event, **kwargs)

    def info(self, event: str, **kwargs) -> None:
        self._record("info", event, **kwargs)

    def warning(self, event: str, **kwargs) -> None:
        self._record("warning", event, **kwargs)

    def error(self, event: str, **kwargs) -> None:
        self._record("error", event, **kwargs)


def _free_port() -> int:
    """Reserve and release a port, returning its number."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestCreateTokenStore:
    """Token store creation must not crash on backends that lack one."""

    @pytest.mark.parametrize(
        ("flag", "value", "backend"),
        [("--maildir", "md", "maildir"), ("--nats", "nats://localhost:4222", "nats")],
    )
    def test_non_sqlite_backend_warns_without_crashing(self, tmp_path, flag, value, backend):
        if flag == "--maildir":
            value = str(tmp_path / value)
        args = parse_args(["serve", flag, value])
        logger = _RecordingLogger()

        assert _create_token_store(args, logger) is None

        level, event, kwargs = logger.calls[-1]
        assert level == "warning"
        assert "%s" not in event
        assert kwargs["backend"] == backend

    def test_no_auth_returns_none(self, tmp_path):
        args = parse_args(["serve", "--db", str(tmp_path / "m.db"), "--no-auth"])
        assert _create_token_store(args, _RecordingLogger()) is None

    def test_sqlite_backend_builds_store(self, tmp_path):
        args = parse_args(["serve", "--db", str(tmp_path / "m.db")])
        assert _create_token_store(args, _RecordingLogger()) is not None


class TestResolveAdminAuth:
    """Admin auth resolution must not crash when auto-generating a password."""

    def test_remote_without_password_autogenerates(self):
        args = parse_args(["serve", "--remote"])
        logger = _RecordingLogger()

        admin_host, admin_password = _resolve_admin_auth(args, logger)

        assert admin_host == "0.0.0.0"
        assert admin_password
        level, event, kwargs = logger.calls[-1]
        assert level == "warning"
        assert "%s" not in event
        assert kwargs["admin_host"] == "0.0.0.0"

    def test_explicit_password_is_kept(self):
        args = parse_args(["serve", "--remote", "--admin-password", "hunter2"])
        admin_host, admin_password = _resolve_admin_auth(args, _RecordingLogger())
        assert (admin_host, admin_password) == ("0.0.0.0", "hunter2")

    def test_localhost_default_has_no_password(self):
        admin_host, admin_password = _resolve_admin_auth(parse_args(["serve"]), _RecordingLogger())
        assert (admin_host, admin_password) == ("127.0.0.1", None)


class TestCheckNetworkExposure:
    """The startup guard weighs the API bind host, not just the admin host."""

    @staticmethod
    def _check(argv: list[str]) -> _RecordingLogger:
        logger = _RecordingLogger()
        _check_network_exposure(parse_args(["serve", *argv]), logger)
        return logger

    def _expect_refusal(self, argv: list[str]) -> dict:
        logger = _RecordingLogger()
        with pytest.raises(SystemExit) as exc:
            _check_network_exposure(parse_args(["serve", *argv]), logger)
        assert exc.value.code == 1
        level, _event, kwargs = logger.calls[-1]
        assert level == "error"
        return kwargs

    def test_no_auth_with_off_host_api_refuses(self):
        kwargs = self._expect_refusal(["--http", "0.0.0.0:8742", "--no-auth"])
        assert kwargs["api_host"] == "0.0.0.0"

    def test_no_auth_with_routable_api_host_refuses(self):
        self._expect_refusal(["--http", "100.115.203.108:8782", "--no-auth"])

    def test_no_auth_with_remote_admin_refuses(self):
        self._expect_refusal(["--remote", "--no-auth"])

    def test_no_auth_on_localhost_is_allowed(self):
        assert self._check(["--http", "127.0.0.1:8742", "--no-auth"]).calls == []

    def test_no_auth_with_bare_port_is_allowed(self):
        """A bare --http port defaults to loopback, so it is not exposed."""
        assert self._check(["--http", "8742", "--no-auth"]).calls == []

    @pytest.mark.parametrize(
        "backend", [["--maildir", "/tmp/mansio-guard-md"], ["--nats", "nats://localhost:4222"]]
    )
    def test_token_storeless_backend_with_off_host_api_refuses(self, backend):
        kwargs = self._expect_refusal([*backend, "--http", "0.0.0.0:8742"])
        assert kwargs["api_host"] == "0.0.0.0"

    def test_token_storeless_backend_on_localhost_is_allowed(self, tmp_path):
        argv = ["--maildir", str(tmp_path / "md"), "--http", "127.0.0.1:8742"]
        assert self._check(argv).calls == []

    def test_token_storeless_backend_with_remote_admin_only_is_allowed(self, tmp_path):
        """The admin panel keeps its own password, so no API is left open."""
        assert self._check(["--maildir", str(tmp_path / "md"), "--remote"]).calls == []

    def test_sqlite_backend_off_host_api_is_allowed(self, tmp_path):
        argv = ["--db", str(tmp_path / "m.db"), "--http", "0.0.0.0:8742"]
        assert self._check(argv).calls == []

    def test_override_flag_warns_instead_of_refusing(self):
        argv = [
            "--http",
            "0.0.0.0:8742",
            "--no-auth",
            "--insecure-allow-unauthenticated-network-api",
        ]
        level, _event, kwargs = self._check(argv).calls[-1]
        assert level == "warning"
        assert kwargs["api_host"] == "0.0.0.0"

    def test_override_flag_defaults_off(self):
        assert parse_args(["serve"]).insecure_allow_unauthenticated_network_api is False


class TestServeStartsUp:
    """End-to-end: ``mansio serve`` reaches a serving state and stays there."""

    @staticmethod
    def _serve_until_healthy(argv: list[str], port: int) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mansio.cli", "serve", *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    output = proc.stdout.read() if proc.stdout else ""
                    pytest.fail(f"serve exited with {proc.returncode}:\n{output}")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                        assert r.status == 200
                        return
                except (urllib.error.URLError, OSError):
                    time.sleep(0.2)
            pytest.fail("serve never became healthy")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    def test_maildir_backend_serves(self, tmp_path):
        port = _free_port()
        self._serve_until_healthy(
            [
                "--maildir",
                str(tmp_path / "md"),
                "--http",
                f"127.0.0.1:{port}",
                "--admin-port",
                str(_free_port()),
            ],
            port,
        )

    def test_remote_without_admin_password_serves(self, tmp_path):
        """The Docker image's default command shape: --remote, no --admin-password."""
        port = _free_port()
        self._serve_until_healthy(
            [
                "--db",
                str(tmp_path / "m.db"),
                "--http",
                f"127.0.0.1:{port}",
                "--remote",
                "--admin-port",
                str(_free_port()),
            ],
            port,
        )
