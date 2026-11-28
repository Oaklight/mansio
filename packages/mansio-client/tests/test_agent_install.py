"""End-to-end tests for ``mansio-agent install`` and ``uninstall``."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from mansio_client.agent import cli, runtimes
from mansio_client.agent.cli import main
from mansio_client.transport import MansioAPIError

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


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.chdir(tmp_path)
    main(list(args))


def test_install_claude_project_writes_entry_and_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(
        tmp_path,
        monkeypatch,
        "install",
        "claude-code",
        "--url",
        "http://localhost:8742",
        "--id",
        "claude-code-demo-1",
    )
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    entry = data["mcpServers"]["mansio"]
    assert entry["command"] == "mansio-mcp"
    assert entry["args"] == []
    assert entry["env"] == {
        "MANSIO_URL": "http://localhost:8742",
        "MANSIO_USER_ID": "claude-code-demo-1",
    }
    hook = data["hooks"]["SessionStart"][0]["hooks"][0]
    assert hook == {
        "type": "command",
        "command": "mansio-agent poll --format claude",
        "timeout": 15,
    }


def test_reinstall_is_idempotent_and_makes_no_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = ("install", "claude-code", "--url", "http://localhost:8742", "--id", "id-demo-1")
    _run(tmp_path, monkeypatch, *args)
    path = tmp_path / ".claude" / "settings.json"
    before = path.read_text(encoding="utf-8")

    _run(tmp_path, monkeypatch, *args)

    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / ".claude" / "settings.json.bak").exists()


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _run(
        tmp_path,
        monkeypatch,
        "install",
        "claude-code",
        "--url",
        "http://localhost:8742",
        "--id",
        "claude-code-demo-1",
        "--dry-run",
    )
    out = capsys.readouterr().out
    assert "+++ " in out
    assert "claude-code-demo-1" in out
    assert not (tmp_path / ".claude").exists()


def test_token_not_written_without_write_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(
        tmp_path,
        monkeypatch,
        "install",
        "claude-code",
        "--url",
        "http://localhost:8742",
        "--id",
        "claude-code-demo-1",
        "--token",
        "secret-token",
    )
    path = tmp_path / ".claude" / "settings.json"
    assert "secret-token" not in path.read_text(encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "MANSIO_TOKEN" not in data["mcpServers"]["mansio"]["env"]


def test_write_token_writes_value_and_private_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(
        tmp_path,
        monkeypatch,
        "install",
        "claude-code",
        "--url",
        "http://localhost:8742",
        "--id",
        "claude-code-demo-1",
        "--token",
        "secret-token",
        "--write-token",
    )
    path = tmp_path / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["mansio"]["env"]["MANSIO_TOKEN"] == "secret-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_dry_run_redacts_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _run(tmp_path, monkeypatch, "install", "claude-code", "--url", "http://x", "--id", "id-demo-1")
    _run(
        tmp_path,
        monkeypatch,
        "install",
        "claude-code",
        "--url",
        "http://x",
        "--id",
        "id-demo-1",
        "--token",
        "secret-token",
        "--write-token",
        "--dry-run",
    )
    out = capsys.readouterr().out
    assert "<redacted>" in out
    assert "secret-token" not in out


def test_install_codex_writes_config_and_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(
        tmp_path,
        monkeypatch,
        "install",
        "codex",
        "--url",
        "http://localhost:8742",
        "--id",
        "codex-demo-1",
    )
    config_text = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.mansio]" in config_text
    assert 'command = "mansio-mcp"' in config_text
    assert 'MANSIO_USER_ID = "codex-demo-1"' in config_text
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    hook = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert hook["command"] == "mansio-agent poll --format codex"


def test_install_codex_preserves_unrelated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('# keep\nmodel = "gpt-5"\n\n[other]\nkeep = "me"\n', encoding="utf-8")

    _run(tmp_path, monkeypatch, "install", "codex", "--url", "http://x", "--id", "codex-demo-1")

    text = path.read_text(encoding="utf-8")
    assert "# keep" in text
    assert 'model = "gpt-5"' in text
    assert "[other]" in text
    assert 'keep = "me"' in text


def test_uninstall_removes_only_our_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}, "other": True}),
        encoding="utf-8",
    )
    _run(tmp_path, monkeypatch, "install", "claude-code", "--url", "http://x", "--id", "id-demo-1")

    _run(tmp_path, monkeypatch, "uninstall", "claude-code")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"] == {"other": {"command": "x"}}
    assert data["other"] is True
    assert "hooks" not in data


def test_invalid_user_id_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path,
            monkeypatch,
            "install",
            "claude-code",
            "--url",
            "http://x",
            "--id",
            "Bad ID",
        )
    assert exc.value.code == 1
    assert "invalid user id" in capsys.readouterr().err


def test_write_token_requires_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path,
            monkeypatch,
            "install",
            "claude-code",
            "--url",
            "http://x",
            "--id",
            "id-demo-1",
            "--write-token",
        )
    assert exc.value.code == 1


def test_token_and_token_env_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANSIO_TOKEN", "env-token")
    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path,
            monkeypatch,
            "install",
            "claude-code",
            "--url",
            "http://x",
            "--id",
            "id-demo-1",
            "--token",
            "secret",
            "--token-env",
        )
    assert exc.value.code == 1


def test_token_env_missing_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path,
            monkeypatch,
            "install",
            "claude-code",
            "--url",
            "http://x",
            "--id",
            "id-demo-1",
            "--token-env",
        )
    assert exc.value.code == 1


def test_user_scope_uses_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _run(
        tmp_path,
        monkeypatch,
        "install",
        "claude-code",
        "--scope",
        "user",
        "--url",
        "http://x",
        "--id",
        "claude-code-demo-1",
    )
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_generated_id_is_reused_on_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(tmp_path, monkeypatch, "install", "claude-code", "--url", "http://x")
    first = runtimes.read_user_id(runtimes.RUNTIMES["claude-code"], "project")
    _run(tmp_path, monkeypatch, "install", "claude-code", "--url", "http://x")
    assert runtimes.read_user_id(runtimes.RUNTIMES["claude-code"], "project") == first


def test_generated_id_avoids_a_collision_in_the_other_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    project_root = tmp_path / "project"
    project_root.mkdir()
    project_id = "claude-code-project-1"
    _run(
        project_root,
        monkeypatch,
        "install",
        "claude-code",
        "--scope",
        "user",
        "--url",
        "http://x",
        "--id",
        project_id,
    )

    _run(project_root, monkeypatch, "install", "claude-code", "--url", "http://x")

    user_id = runtimes.read_user_id(runtimes.RUNTIMES["claude-code"], "project", cwd=project_root)
    assert user_id is not None
    assert user_id != project_id
    assert user_id.endswith("-2")


def test_create_channel_403_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def deny(self, *args, **kwargs):
        raise MansioAPIError(403, "denied")

    monkeypatch.setattr(cli.HttpTransport, "channel_create", deny)

    with pytest.raises(SystemExit) as exc:
        _run(
            tmp_path,
            monkeypatch,
            "install",
            "claude-code",
            "--url",
            "http://x",
            "--id",
            "claude-code-demo-1",
            "--create-channel",
        )
    assert exc.value.code == 1
    assert "permission denied" in capsys.readouterr().err
