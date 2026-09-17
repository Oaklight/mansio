"""Tests for MansioClient poll cursor persistence.

Verifies:
- a cursor is stored as soon as a poll advances it, so a client that is
  killed without closing keeps its position
- two sessions sharing a user ID do not roll back each other's position
- cursors for different channels survive concurrent sessions
- a failed cursor write is reported and retried
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import textwrap
import time

import pytest
from mansio_client import MansioClient

POLL_AND_WAIT = """
import sys, time
from mansio_client import MansioClient

client = MansioClient(sys.argv[1], sys.argv[2])
msgs = client.channel_poll(sys.argv[3])
print(len(msgs), flush=True)
time.sleep(300)
"""


def _send(url: str, channel: str, payloads: list[str]) -> None:
    with MansioClient(url, "cursor-writer") as writer:
        for payload in payloads:
            writer.channel_send(channel, payload)


def test_cursor_survives_client_killed_with_sigterm(server_url):
    channel = "cursor-sigterm"
    _send(server_url, channel, ["m0", "m1", "m2"])

    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(POLL_AND_WAIT), server_url,
         "killed-agent", channel],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "3"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on assertion failure
            proc.kill()
            proc.wait(timeout=10)

    with MansioClient(server_url, "killed-agent") as restarted:
        assert restarted.channel_poll(channel) == []


def test_concurrent_sessions_do_not_roll_back_each_other(server_url):
    channel = "cursor-shared"
    _send(server_url, channel, ["b0"])

    session_one = MansioClient(server_url, "shared-agent")
    session_two = MansioClient(server_url, "shared-agent")

    assert [m.payload for m in session_one.channel_poll(channel)] == ["b0"]
    session_two.channel_poll(channel)

    _send(server_url, channel, ["b1"])
    assert [m.payload for m in session_two.channel_poll(channel)] == ["b1"]

    # The session with the older position closes last.
    session_two.close()
    session_one.close()

    with MansioClient(server_url, "shared-agent") as session_three:
        assert session_three.channel_poll(channel) == []


def test_sessions_polling_different_channels_keep_both_cursors(server_url):
    _send(server_url, "cursor-alpha", ["a0"])
    _send(server_url, "cursor-beta", ["b0"])

    session_one = MansioClient(server_url, "split-agent")
    session_two = MansioClient(server_url, "split-agent")
    session_one.channel_poll("cursor-alpha")
    session_two.channel_poll("cursor-beta")
    session_one.close()
    session_two.close()

    with MansioClient(server_url, "split-agent") as later:
        assert later.channel_poll("cursor-alpha") == []
        assert later.channel_poll("cursor-beta") == []


def test_failed_cursor_write_is_logged_and_retried(server_url, caplog):
    channel = "cursor-retry"
    _send(server_url, channel, ["m0"])

    client = MansioClient(server_url, "flaky-agent")
    real_publish = client._transport.publish
    calls: list[int] = []

    def failing_publish(*args, **kwargs):
        calls.append(1)
        raise ConnectionError("cursor write rejected")

    client._transport.publish = failing_publish
    with caplog.at_level(logging.WARNING, logger="mansio_client.client"):
        assert [m.payload for m in client.channel_poll(channel)] == ["m0"]
    assert calls
    assert "failed to save poll cursors" in caplog.text

    client._transport.publish = real_publish
    client.close()

    with MansioClient(server_url, "flaky-agent") as later:
        assert later.channel_poll(channel) == []


def test_restore_failure_propagates(server_url, monkeypatch):
    from mansio_client import client as client_module

    def failing_query(self, *args, **kwargs):
        raise ConnectionError("query rejected")

    monkeypatch.setattr(
        client_module.HttpTransport, "query", failing_query, raising=True
    )
    with pytest.raises(ConnectionError):
        MansioClient(server_url, "broken-agent")


def test_poll_persists_without_close(server_url):
    channel = "cursor-no-close"
    _send(server_url, channel, ["m0", "m1"])

    leaked = MansioClient(server_url, "no-close-agent")
    assert len(leaked.channel_poll(channel)) == 2
    # Deliberately no close(): the cursor must already be on the server.
    time.sleep(0.05)

    with MansioClient(server_url, "no-close-agent") as fresh:
        assert fresh.channel_poll(channel) == []
