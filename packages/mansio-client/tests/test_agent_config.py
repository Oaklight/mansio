"""Tests for mansio-agent config merging and atomic writes."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from mansio_client.agent import config


def test_merge_claude_preserves_unknown_keys_and_is_idempotent() -> None:
    root = {
        "mcpServers": {"other": {"command": "x"}},
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "mansio-agent poll --format claude",
                            "timeout": 1,
                            "statusMessage": "keep",
                        },
                        {"type": "command", "command": "other-tool"},
                    ],
                }
            ]
        },
        "permissions": {"allow": ["Bash"]},
    }

    config.merge_claude_settings(
        root, url="http://localhost:8742", user_id="claude-code-demo-1", token=None
    )
    first = json.dumps(root, sort_keys=True)
    config.merge_claude_settings(
        root, url="http://localhost:8742", user_id="claude-code-demo-1", token=None
    )

    assert json.dumps(root, sort_keys=True) == first
    assert root["mcpServers"]["other"] == {"command": "x"}
    assert root["permissions"] == {"allow": ["Bash"]}
    entry = root["mcpServers"]["mansio"]
    assert entry["command"] == "mansio-mcp"
    assert entry["args"] == []
    assert entry["env"] == {
        "MANSIO_URL": "http://localhost:8742",
        "MANSIO_USER_ID": "claude-code-demo-1",
    }
    hooks = root["hooks"]["SessionStart"][0]["hooks"]
    assert hooks[0]["statusMessage"] == "keep"
    assert hooks[1] == {"type": "command", "command": "other-tool"}


def test_remove_claude_settings_removes_only_mansio() -> None:
    root = {
        "mcpServers": {
            "mansio": {"command": "mansio-mcp"},
            "other": {"command": "x"},
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {"type": "command", "command": "mansio-agent poll --format claude"},
                        {"type": "command", "command": "other-tool"},
                    ],
                }
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "keep"}]}],
        },
    }

    config.remove_claude_settings(root)

    assert root["mcpServers"] == {"other": {"command": "x"}}
    hooks = root["hooks"]["SessionStart"][0]["hooks"]
    assert hooks == [{"type": "command", "command": "other-tool"}]
    assert root["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "keep"}]}]


def test_token_is_written_or_removed_as_a_managed_key() -> None:
    root: dict = {}
    config.merge_claude_settings(root, url="u", user_id="i", token="secret")
    assert root["mcpServers"]["mansio"]["env"]["MANSIO_TOKEN"] == "secret"
    config.merge_claude_settings(root, url="u", user_id="i", token=None)
    assert "MANSIO_TOKEN" not in root["mcpServers"]["mansio"]["env"]


def test_write_if_changed_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    result = config.write_if_changed(path, "{}\n", dry_run=True)

    assert result.changed is True
    assert result.diff
    assert not path.exists()


def test_write_if_changed_backup_only_on_change(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}\n", encoding="utf-8")

    unchanged = config.write_if_changed(path, "{}\n")
    assert unchanged.changed is False
    assert not (tmp_path / "config.json.bak").exists()

    changed = config.write_if_changed(path, '{"a": 1}\n')
    assert changed.changed is True
    assert (tmp_path / "config.json.bak").read_text(encoding="utf-8") == "{}\n"
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n'


def test_write_with_secret_sets_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config.write_if_changed(path, '{"MANSIO_TOKEN": "secret"}\n', secrets=("secret",))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_redact_hides_tokens_in_both_formats() -> None:
    text = 'MANSIO_TOKEN = "secret"\n{"MANSIO_TOKEN": "other"}'
    redacted = config.redact(text, ("secret",))
    assert "secret" not in redacted
    assert "other" not in redacted
    assert "<redacted>" in redacted


def test_project_slug_and_generated_ids() -> None:
    assert config.project_slug(Path("/tmp/My Project!")) == "my-project"
    used = {"claude-code-my-project-1"}
    assert config.generate_user_id("claude-code", "my-project", used) == (
        "claude-code-my-project-2"
    )
    assert config.is_valid_user_id("claude-code-my-project-2")
    assert not config.is_valid_user_id("Claude Code")
    assert not config.is_valid_user_id("-bad-")


def test_hook_merge_replaces_only_the_managed_entry() -> None:
    root = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {"type": "command", "command": "mansio-agent poll --format claude"},
                        {"type": "command", "command": "other-tool"},
                    ],
                }
            ]
        }
    }
    config.merge_session_start_hook(root, "mansio-agent poll --format codex")
    hooks = root["hooks"]["SessionStart"][0]["hooks"]
    assert hooks[0]["command"] == "mansio-agent poll --format codex"
    assert hooks[1]["command"] == "other-tool"


def test_hook_merge_collapses_duplicate_managed_entries() -> None:
    root = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "mansio-agent poll --format claude",
                            "statusMessage": "keep",
                        },
                        {"type": "command", "command": "other-tool"},
                    ],
                },
                {
                    "matcher": "other",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "mansio-agent poll --format codex",
                        }
                    ],
                },
            ]
        }
    }

    config.merge_session_start_hook(root, "mansio-agent poll --format claude")

    groups = root["hooks"]["SessionStart"]
    assert len(groups) == 1
    managed = [hook for hook in groups[0]["hooks"] if config.is_mansio_poll_hook(hook)]
    assert len(managed) == 1
    assert managed[0]["command"] == "mansio-agent poll --format claude"
    assert managed[0]["statusMessage"] == "keep"
    assert any(hook.get("command") == "other-tool" for hook in groups[0]["hooks"])
