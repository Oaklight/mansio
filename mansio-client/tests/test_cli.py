"""Tests for mansio-client CLI flag handling and --agent deprecation."""

from __future__ import annotations

import warnings

import pytest

from mansio_client import MansioAPIError
from mansio_client._vendor.httpclient import HttpConnectionError
from mansio_client.cli import _build_parser, _msg_dict, main
from mansio_client.types import Message


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
        args = parser.parse_args(
            ["send", "-c", "general", "--intent", "FYI_ONLY", "hello"]
        )
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
