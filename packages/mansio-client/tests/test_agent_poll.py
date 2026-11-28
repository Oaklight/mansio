"""Tests for the ``mansio-agent poll`` command."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mansio_client.agent import cli, poll, runtimes

_ENV_NAMES = (
    "MANSIO_URL",
    "MANSIO_USER_ID",
    "MANSIO_TOKEN",
    "MANSIO_CHANNELS",
    "MANSIO_AGENT_ID",
    "PIAZZA_URL",
    "PIAZZA_AGENT_ID",
    "PIAZZA_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


class _FakeClient:
    def __init__(self, url: str, user_id: str, token: str | None = None) -> None:
        self.url = url
        self.user_id = user_id
        self.token = token
        self.polled: list[str] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def channel_list(self, *, detail: bool = False) -> list[str]:
        return ["general", "_system:agents"]

    def channel_poll(self, channel: str, *, include_own: bool = False) -> list:
        self.polled.append(channel)
        if channel == "general":
            return [SimpleNamespace(sender="alice", payload="hello")]
        return []


def test_parse_channels() -> None:
    assert poll.parse_channels(None) is None
    assert poll.parse_channels(" a, b, ,c ") == ["a", "b", "c"]


def test_parse_channels_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANSIO_CHANNELS", "one,two")
    assert poll.parse_channels(None) == ["one", "two"]


def test_format_context_cases() -> None:
    assert poll.format_context([], []) == "[mansio] No channels found."
    assert poll.format_context(["general"], []) == "[mansio] No new messages."
    context = poll.format_context(
        ["general"], [("general", SimpleNamespace(sender="alice", payload="hello"))]
    )
    assert "--- #general ---" in context
    assert "alice: hello" in context


def test_render_shapes() -> None:
    context = "[mansio] No new messages."
    for fmt in ("claude", "codex"):
        payload = json.loads(poll.render(fmt, context))
        assert payload["hookSpecificOutput"]["additionalContext"] == context
    assert poll.render("plain", context) == context


def test_collect_skips_system_channels() -> None:
    client = _FakeClient("http://x", "agent-1")
    channels, messages = poll.collect(client, None)
    assert channels == ["general"]
    assert client.polled == ["general"]
    assert messages[0][0] == "general"


def test_run_with_fake_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MANSIO_URL", "http://x")
    monkeypatch.setenv("MANSIO_USER_ID", "agent-1")
    monkeypatch.setattr(poll, "MansioClient", _FakeClient)

    code = poll.run(poll_format="claude", channels=None, token=None)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "general" in context
    assert "alice: hello" in context


def test_run_without_connection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    code = poll.run(poll_format="plain", channels=None, token=None)
    assert code == 1
    assert "MANSIO_URL and MANSIO_USER_ID" in capsys.readouterr().err


def test_resolve_connection_prefers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from mansio_client.agent.cli import main

    main(
        [
            "install",
            "claude-code",
            "--url",
            "http://config",
            "--id",
            "claude-code-demo-1",
        ]
    )
    assert runtimes.resolve_connection("claude", cwd=tmp_path) == (
        "http://config",
        "claude-code-demo-1",
    )

    monkeypatch.setenv("MANSIO_URL", "http://env")
    monkeypatch.setenv("MANSIO_USER_ID", "agent-env-1")
    assert runtimes.resolve_connection("claude", cwd=tmp_path) == (
        "http://env",
        "agent-env-1",
    )


def _install_with_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cli.main(
        [
            "install",
            "claude-code",
            "--url",
            "http://config",
            "--id",
            "claude-code-demo-1",
            "--token",
            "config-token",
            "--write-token",
        ]
    )


def _recording_client(monkeypatch: pytest.MonkeyPatch) -> list[_FakeClient]:
    made: list[_FakeClient] = []

    class _RecordingClient(_FakeClient):
        def __init__(self, url: str, user_id: str, token: str | None = None) -> None:
            super().__init__(url, user_id, token)
            made.append(self)

    monkeypatch.setattr(poll, "MansioClient", _RecordingClient)
    return made


def test_token_falls_back_to_the_installed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_with_token(tmp_path, monkeypatch)
    made = _recording_client(monkeypatch)

    assert poll.run(poll_format="claude", channels=None, token=None) == 0
    assert made[-1].token == "config-token"


def test_token_environment_wins_over_the_installed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_with_token(tmp_path, monkeypatch)
    monkeypatch.setenv("MANSIO_TOKEN", "env-token")
    made = _recording_client(monkeypatch)

    assert poll.run(poll_format="claude", channels=None, token=None) == 0
    assert made[-1].token == "env-token"


def test_token_flag_wins_over_environment_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_with_token(tmp_path, monkeypatch)
    monkeypatch.setenv("MANSIO_TOKEN", "env-token")
    made = _recording_client(monkeypatch)

    assert poll.run(poll_format="claude", channels=None, token="flag-token") == 0
    assert made[-1].token == "flag-token"
