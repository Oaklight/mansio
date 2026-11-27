"""Tests for mansio-client CLI flag handling and --agent deprecation."""

from __future__ import annotations

import warnings

import pytest
from mansio_client import MansioAPIError
from mansio_client._vendor.httpclient import HttpConnectionError
from mansio_client.cli import _build_parser, _msg_dict, main
from mansio_core.types import ALLOWED_PERMISSIONS, Message


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


class TestIntentFlags:
    """--intent on send and read is parsed and defaults to None."""

    def test_send_intent_flag(self, monkeypatch):
        monkeypatch.delenv("MANSIO_URL", raising=False)
        parser = _build_parser()
        args = parser.parse_args(["send", "-c", "general", "--intent", "FYI_ONLY", "hello"])
        assert args.intent == "FYI_ONLY"
        assert args.message == "hello"

    def test_send_intent_defaults_to_none(self, monkeypatch):
        monkeypatch.delenv("MANSIO_URL", raising=False)
        parser = _build_parser()
        args = parser.parse_args(["send", "-c", "general", "hello"])
        assert args.intent is None

    def test_read_intent_flag(self, monkeypatch):
        monkeypatch.delenv("MANSIO_URL", raising=False)
        parser = _build_parser()
        args = parser.parse_args(["read", "-c", "general", "--intent", "FYI_ONLY"])
        assert args.intent == "FYI_ONLY"


class TestMsgDict:
    """CLI JSON output keeps threading and intent fields."""

    def test_includes_optional_fields(self):
        msg = Message(
            id="msg-1",
            channel="general",
            sender="user-a",
            msg_type="chat",
            payload="hello",
            timestamp="2026-01-01T00:00:00Z",
            parent_id="msg-0",
            thread_id="msg-0",
            intent="DIRECT_QUESTION",
        )
        d = _msg_dict(msg)
        assert d["parent_id"] == "msg-0"
        assert d["thread_id"] == "msg-0"
        assert d["intent"] == "DIRECT_QUESTION"

    def test_omits_absent_optional_fields(self):
        msg = Message(
            id="msg-1",
            channel="general",
            sender="user-a",
            msg_type="chat",
            payload="hello",
            timestamp="2026-01-01T00:00:00Z",
        )
        d = _msg_dict(msg)
        assert "parent_id" not in d
        assert "thread_id" not in d
        assert "intent" not in d


class _FailingClient:
    """Client stand-in whose first operation raises *error*."""

    def __init__(self, error):
        self.error = error
        self.closed = False

    def channel_list(self):
        raise self.error

    def close(self):
        self.closed = True


class TestErrorReporting:
    """API and transport failures become one clean error line, not a traceback."""

    def _run(self, monkeypatch, capsys, error):
        import mansio_client.cli as cli

        fake = _FailingClient(error)
        monkeypatch.setattr(cli, "MansioClient", lambda *a, **k: fake)
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "-s",
                    "http://example.invalid",
                    "--user-id",
                    "test-agent",
                    "channels",
                ]
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Traceback" not in err
        assert fake.closed is True
        return err

    def test_api_error(self, monkeypatch, capsys):
        err = self._run(monkeypatch, capsys, MansioAPIError(401, "bad token"))
        assert "error: HTTP 401: bad token" in err

    def test_connection_error(self, monkeypatch, capsys):
        err = self._run(monkeypatch, capsys, HttpConnectionError("Connection refused"))
        assert "error: cannot reach http://example.invalid: Connection refused" in err

    def test_invalid_user_id(self, monkeypatch, capsys):
        # The real client rejects the ID before any network call.
        monkeypatch.delenv("MANSIO_URL", raising=False)
        with pytest.raises(SystemExit) as exc:
            main(["-s", "http://127.0.0.1:1", "--user-id", "INVALID", "channels"])
        assert exc.value.code == 1
        assert "error: Invalid user_id" in capsys.readouterr().err


class TestIncludeOwnFlags:
    """poll and read exclude this agent's messages unless --include-own."""

    def test_poll_default_excludes_own(self):
        parser = _build_parser()
        args = parser.parse_args(["poll", "-c", "general"])
        assert args.include_own is False

    def test_poll_include_own(self):
        parser = _build_parser()
        args = parser.parse_args(["poll", "-c", "general", "--include-own"])
        assert args.include_own is True

    def test_read_default_excludes_own(self):
        parser = _build_parser()
        args = parser.parse_args(["read", "-c", "general"])
        assert args.include_own is False

    def test_read_include_own(self):
        parser = _build_parser()
        args = parser.parse_args(["read", "-c", "general", "--include-own"])
        assert args.include_own is True


class TestFollowFlag:
    """poll --follow is opt-in and off by default."""

    def test_poll_follow_defaults_to_false(self):
        parser = _build_parser()
        args = parser.parse_args(["poll", "-c", "general"])
        assert args.follow is False

    def test_poll_follow_is_parsed(self):
        parser = _build_parser()
        args = parser.parse_args(["poll", "-c", "general", "--follow"])
        assert args.follow is True


class TestAclPermissionVocabulary:
    """acl add accepts presets, verbs, and legacy levels; rejects the rest."""

    @pytest.mark.parametrize("permission", sorted(ALLOWED_PERMISSIONS))
    def test_permission_value_parses(self, permission):
        parser = _build_parser()
        args = parser.parse_args(["acl", "add", "general", "user-a", "--permission", permission])
        assert args.permission == permission

    def test_default_is_read(self):
        parser = _build_parser()
        args = parser.parse_args(["acl", "add", "general", "user-a"])
        assert args.permission == "read"

    def test_help_names_presets_and_legacy_levels(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["acl", "add", "--help"])
        assert exc.value.code == 0
        text = " ".join(capsys.readouterr().out.split())
        assert "viewer/member/owner" in text
        assert "write and admin map to member and owner" in text

    def test_bogus_value_rejected(self, capsys):
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["acl", "add", "general", "user-a", "--permission", "bogus"])
        assert exc.value.code == 2
        assert "invalid choice" in capsys.readouterr().err
