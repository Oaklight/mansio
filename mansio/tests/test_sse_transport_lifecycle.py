"""Tests for HttpTransport SSE callback errors and stream shutdown.

Verifies:
- An exception raised by a subscribe() callback is logged with its traceback
- A subscription's on_error hook receives the message and the exception
- One failing callback doesn't stop the others from receiving the message
- close() returns promptly while a subscription is open
- unsubscribe() of the last subscription stops the SSE stream
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend
from mansio.transport_http import HttpTransport

#: Generous upper bound for an operation that must not wait on the server's
#: SSE keepalive; the bug being guarded against took over ten seconds.
PROMPT_SECONDS = 1.0


@pytest.fixture()
def server_with_bus():
    """Start a server on an OS-assigned port, yield its URL and Bus."""
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    yield f"http://{host}:{port}", bus

    server.shutdown()


class TestCallbackErrors:
    """Exceptions raised by subscriber callbacks must not vanish."""

    def test_callback_exception_is_logged(
        self, server_with_bus: tuple, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising callback is reported with its traceback, not swallowed."""
        url, bus = server_with_bus
        transport = HttpTransport(url, user_id="err-agent")
        called = threading.Event()

        def boom(msg) -> None:
            called.set()
            raise RuntimeError("callback exploded")

        with caplog.at_level(logging.ERROR, logger="mansio.transport_http"):
            transport.subscribe("err-ch", boom)
            time.sleep(0.5)
            bus.publish("err-ch", "sender", "chat", "trigger")
            assert called.wait(timeout=5), "callback never ran"
            time.sleep(0.2)
            transport.close()

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "failing callback produced no log record"
        assert any(r.exc_info is not None for r in errors), "no traceback logged"
        assert any("callback exploded" in r.exc_text for r in errors if r.exc_text)

    def test_on_error_hook_receives_failure(self, server_with_bus: tuple) -> None:
        """An on_error hook gets the message and the exception."""
        url, bus = server_with_bus
        transport = HttpTransport(url, user_id="hook-agent")
        failures: list[tuple] = []
        reported = threading.Event()

        def boom(msg) -> None:
            raise ValueError("no good")

        def on_error(msg, exc) -> None:
            failures.append((msg, exc))
            reported.set()

        transport.subscribe("hook-ch", boom, on_error=on_error)
        time.sleep(0.5)
        bus.publish("hook-ch", "sender", "chat", "payload here")

        assert reported.wait(timeout=5), "on_error was never called"
        transport.close()

        msg, exc = failures[0]
        assert msg.channel == "hook-ch"
        assert msg.payload == "payload here"
        assert isinstance(exc, ValueError)
        assert str(exc) == "no good"

    def test_failing_callback_does_not_block_others(
        self, server_with_bus: tuple, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising subscriber doesn't cost the other subscribers the message."""
        url, bus = server_with_bus
        transport = HttpTransport(url, user_id="multi-agent")
        delivered: list = []
        got_it = threading.Event()

        def boom(msg) -> None:
            raise RuntimeError("first one fails")

        def collect(msg) -> None:
            delivered.append(msg)
            got_it.set()

        with caplog.at_level(logging.ERROR, logger="mansio.transport_http"):
            transport.subscribe("multi-ch", boom)
            transport.subscribe("multi-ch", collect)
            time.sleep(0.5)
            bus.publish("multi-ch", "sender", "chat", "still arrives")

            assert got_it.wait(timeout=5)
            transport.close()

        assert [m.payload for m in delivered] == ["still arrives"]


class TestPromptShutdown:
    """close() and unsubscribe() must not wait for the server's keepalive."""

    def test_close_with_active_subscription_is_prompt(self, server_with_bus: tuple) -> None:
        """close() returns quickly even with an idle SSE stream open."""
        url, _bus = server_with_bus
        transport = HttpTransport(url, user_id="close-agent")
        transport.subscribe("close-ch", lambda msg: None)
        time.sleep(1.0)  # let the stream connect and block on its read

        start = time.perf_counter()
        transport.close()
        elapsed = time.perf_counter() - start

        assert elapsed < PROMPT_SECONDS, f"close() took {elapsed:.2f}s"

    def test_close_after_message_is_prompt(self, server_with_bus: tuple) -> None:
        """close() is prompt after the stream has delivered a message."""
        url, bus = server_with_bus
        transport = HttpTransport(url, user_id="close-agent-2")
        got_it = threading.Event()
        transport.subscribe("close-ch-2", lambda msg: got_it.set())
        time.sleep(0.5)
        bus.publish("close-ch-2", "sender", "chat", "hi")
        assert got_it.wait(timeout=5)

        start = time.perf_counter()
        transport.close()
        elapsed = time.perf_counter() - start

        assert elapsed < PROMPT_SECONDS, f"close() took {elapsed:.2f}s"

    def test_close_without_subscription_is_prompt(self, server_with_bus: tuple) -> None:
        """close() on an unsubscribed transport still works and is prompt."""
        url, _bus = server_with_bus
        transport = HttpTransport(url, user_id="close-agent-3")

        start = time.perf_counter()
        transport.close()
        assert time.perf_counter() - start < PROMPT_SECONDS

    def test_unsubscribe_last_subscription_stops_stream(self, server_with_bus: tuple) -> None:
        """Dropping the last subscription tears the SSE stream down promptly."""
        url, _bus = server_with_bus
        transport = HttpTransport(url, user_id="unsub-agent")
        sub_id = transport.subscribe("unsub-ch", lambda msg: None)
        time.sleep(1.0)
        assert transport._sse_thread is not None

        start = time.perf_counter()
        transport.unsubscribe(sub_id)
        elapsed = time.perf_counter() - start

        assert elapsed < PROMPT_SECONDS, f"unsubscribe() took {elapsed:.2f}s"
        assert transport._sse_thread is None
        transport.close()

    def test_unsubscribe_keeps_other_channels_live(self, server_with_bus: tuple) -> None:
        """Remaining channels keep receiving after another is unsubscribed."""
        url, bus = server_with_bus
        transport = HttpTransport(url, user_id="unsub-agent-2")
        delivered: list = []
        got_it = threading.Event()

        def collect(msg) -> None:
            delivered.append(msg)
            got_it.set()

        drop_id = transport.subscribe("drop-ch", lambda msg: None)
        transport.subscribe("keep-ch", collect)
        time.sleep(1.0)

        transport.unsubscribe(drop_id)
        time.sleep(1.0)  # let the stream reopen with the remaining channel

        bus.publish("keep-ch", "sender", "chat", "still listening")
        assert got_it.wait(timeout=5), "remaining channel stopped receiving"
        assert delivered[0].payload == "still listening"
        transport.close()
