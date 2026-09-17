"""Tests for mansio-client CLI flag handling and --agent deprecation."""

from __future__ import annotations

import warnings

import pytest

from mansio_client.cli import _build_parser, main


class TestFlagParsing:
    """Test --user-id and --agent flag handling."""

    def test_user_id_flag_parsed(self, monkeypatch):
        monkeypatch.delenv("MANSIO_USER_ID", raising=False)
        monkeypatch.delenv("MANSIO_AGENT_ID", raising=False)
        parser = _build_parser()
        args = parser.parse_args(["--user-id", "test-agent", "channels"])
        assert args.user_id == "test-agent"

    def test_agent_flag_parsed(self, monkeypatch):
        monkeypatch.delenv("MANSIO_USER_ID", raising=False)
        monkeypatch.delenv("MANSIO_AGENT_ID", raising=False)
        parser = _build_parser()
        args = parser.parse_args(["--agent", "test-agent", "channels"])
        assert args.agent == "test-agent"

    def test_user_id_from_env(self, monkeypatch):
        monkeypatch.setenv("MANSIO_USER_ID", "env-agent")
        monkeypatch.delenv("MANSIO_AGENT_ID", raising=False)
        parser = _build_parser()
        args = parser.parse_args(["channels"])
        assert args.user_id == "env-agent"

    def test_agent_flag_triggers_deprecation_in_main(self, monkeypatch):
        monkeypatch.setenv("MANSIO_URL", "http://localhost:9999")
        monkeypatch.delenv("MANSIO_USER_ID", raising=False)
        monkeypatch.delenv("MANSIO_AGENT_ID", raising=False)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with pytest.raises((SystemExit, Exception)):
                main(["--agent", "test-agent", "channels"])
        assert any("--agent is deprecated" in str(x.message) for x in w)

    def test_error_message_references_user_id(self, monkeypatch, capsys):
        monkeypatch.delenv("MANSIO_URL", raising=False)
        monkeypatch.delenv("MANSIO_USER_ID", raising=False)
        monkeypatch.delenv("MANSIO_AGENT_ID", raising=False)
        monkeypatch.delenv("PIAZZA_URL", raising=False)
        monkeypatch.delenv("PIAZZA_AGENT_ID", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            main(["channels"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "MANSIO_USER_ID" in captured.err
        assert "--user-id" in captured.err
