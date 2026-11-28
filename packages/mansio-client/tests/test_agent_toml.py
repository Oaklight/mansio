"""Tests for the dependency-free TOML table editor."""

from __future__ import annotations

import pytest
from mansio_client.agent import toml_edit

BASE = """# top comment
model = "gpt-5"

[other.table]
keep = "me"  # inline comment

[mcp_servers.mansio]
command = "old"
args = ["--old"]
custom = "keep"

[mcp_servers.mansio.env]
MANSIO_URL = "http://old"
MANSIO_USER_ID = "old-id"
MANSIO_TOKEN = "old-token"
CUSTOM = "keep"
"""


def _upsert(text: str, *, token: str | None = None) -> str:
    env = {"MANSIO_URL": "http://new", "MANSIO_USER_ID": "new-id"}
    remove = {"MANSIO_TOKEN"}
    if token is not None:
        env["MANSIO_TOKEN"] = token
        remove = set()
    return toml_edit.upsert_server(
        text,
        command="mansio-mcp",
        args=[],
        env=env,
        remove_env_keys=remove,
    )


def test_upsert_creates_tables_for_a_new_file() -> None:
    text = _upsert("")
    assert text.startswith("[mcp_servers.mansio]\n")
    assert 'command = "mansio-mcp"' in text
    assert "args = []" in text
    assert "[mcp_servers.mansio.env]" in text
    assert 'MANSIO_URL = "http://new"' in text


def test_upsert_preserves_comments_and_unknown_keys() -> None:
    text = _upsert(BASE)
    assert "# top comment" in text
    assert 'keep = "me"  # inline comment' in text
    assert 'custom = "keep"' in text
    assert 'CUSTOM = "keep"' in text
    assert 'command = "mansio-mcp"' in text
    assert 'MANSIO_USER_ID = "new-id"' in text
    assert "MANSIO_TOKEN" not in text


def test_upsert_is_byte_idempotent() -> None:
    once = _upsert(BASE)
    twice = _upsert(once)
    assert once == twice


def test_upsert_can_write_a_token() -> None:
    text = _upsert(BASE, token="secret")
    assert 'MANSIO_TOKEN = "secret"' in text
    assert "old-token" not in text


def test_remove_server_preserves_everything_else() -> None:
    text = toml_edit.remove_server(BASE)
    assert "[mcp_servers.mansio" not in text
    assert "# top comment" in text
    assert "[other.table]" in text
    assert 'keep = "me"' in text


def test_inline_and_dotted_forms_are_rejected() -> None:
    with pytest.raises(toml_edit.TomlEditError, match=r"\[mcp_servers\.mansio\]"):
        _upsert('mcp_servers.mansio = { command = "x" }\n')
    with pytest.raises(toml_edit.TomlEditError, match=r"\[mcp_servers\.mansio\]"):
        _upsert('mcp_servers.mansio.command = "x"\n')
    with pytest.raises(toml_edit.TomlEditError, match=r"\[mcp_servers\.mansio\]"):
        _upsert('[mcp_servers]\nmansio = { command = "x" }\n')
    with pytest.raises(toml_edit.TomlEditError, match="mcp_servers"):
        _upsert('mcp_servers = { mansio = { command = "x" } }\n')
    with pytest.raises(toml_edit.TomlEditError, match="mcp_servers"):
        _upsert("mcp_servers = {}\n")


def test_tomllib_parses_the_edited_file() -> None:
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads(_upsert(BASE))
    assert data["mcp_servers"]["mansio"]["command"] == "mansio-mcp"
    assert data["mcp_servers"]["mansio"]["env"]["MANSIO_USER_ID"] == "new-id"
    assert data["other"]["table"]["keep"] == "me"
