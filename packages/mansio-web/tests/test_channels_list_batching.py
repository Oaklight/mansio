"""GET /v1/channels filters in a fixed number of statements and off the event loop."""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request

import pytest
from mansio_core import Bus, SQLiteBackend
from mansio_core.token_store import TokenStore
from mansio_core.types import ACLEntry
from mansio_web import MansioServer
from mansio_web.frontends import HttpFrontend

READER = "reader-agent"
OWNER = "owner-agent"

VISIBLE = (
    [f"pub-{i:02d}" for i in range(40)]
    + [f"regpub-{i:02d}" for i in range(10)]
    + [f"privacl-{i:02d}" for i in range(10)]
    + ["dm:owner-agent:reader-agent"]
)
HIDDEN = [f"privnone-{i:02d}" for i in range(5)] + [
    "dm:owner-agent:third-agent",
    "notebook:owner-agent",
]


@pytest.fixture()
def listing_server(tmp_path):
    bus = Bus(backend=SQLiteBackend(str(tmp_path / "listing.db")))
    for name in [f"pub-{i:02d}" for i in range(40)]:
        bus.publish(name, OWNER, "chat", "x")
    for name in [f"regpub-{i:02d}" for i in range(10)]:
        bus.create_channel(name, OWNER, visibility="public")
        bus.publish(name, OWNER, "chat", "x")
    for name in [f"privacl-{i:02d}" for i in range(10)]:
        bus.create_channel(name, OWNER, visibility="private")
        bus.add_acl_entry(
            ACLEntry(
                channel=name,
                user_id=READER,
                permission="read",
                granted_at="2026-01-01T00:00:00+00:00",
                granted_by=OWNER,
            )
        )
        bus.publish(name, OWNER, "chat", "x")
    for name in [f"privnone-{i:02d}" for i in range(5)]:
        bus.create_channel(name, OWNER, visibility="private")
        bus.publish(name, OWNER, "chat", "x")
    bus.publish("dm:owner-agent:reader-agent", OWNER, "chat", "x")
    bus.publish("dm:owner-agent:third-agent", OWNER, "chat", "x")
    bus.publish("notebook:owner-agent", OWNER, "chat", "x")

    store = TokenStore(str(tmp_path / "tokens.db"))
    token = store.create_token(user_id=READER, label="reader")["token"]
    frontend = HttpFrontend(host="127.0.0.1", port=0, token_store=store)
    server = MansioServer(bus)
    server.add_frontend(frontend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(100):
        if frontend.address[1]:
            break
        time.sleep(0.02)
    url = f"http://{frontend.address[0]}:{frontend.address[1]}"
    yield url, bus, token
    server.shutdown()
    bus.close()


def _get(url: str, token: str, path: str) -> tuple[int, str]:
    request = urllib.request.Request(url + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _channel_statements(bus: Bus, call) -> list[str]:
    statements: list[str] = []
    bus._backend._conn.set_trace_callback(statements.append)
    try:
        call()
    finally:
        bus._backend._conn.set_trace_callback(None)
    return [
        statement
        for statement in statements
        if "FROM channels" in statement
        or "channel_acl" in statement
        or "FROM messages" in statement
    ]


def test_detail_listing_uses_three_statements(listing_server) -> None:
    url, bus, token = listing_server
    seen: dict[str, str] = {}

    def call() -> None:
        status, body = _get(url, token, "/v1/channels?detail=true")
        seen["body"] = body
        assert status == 200, body

    statements = _channel_statements(bus, call)

    assert len(statements) == 3, statements
    assert sum("GROUP BY channel" in statement for statement in statements) == 1
    assert sum("FROM channels WHERE name IN" in statement for statement in statements) == 1
    assert sum("FROM channel_acl WHERE user_id" in statement for statement in statements) == 1
    # The single-channel lookup must not run per channel.
    assert not any("WHERE name = ?" in statement for statement in statements), statements

    names = {channel["name"] for channel in json.loads(seen["body"])["channels"]}
    assert names == set(VISIBLE)


def test_plain_listing_uses_three_statements(listing_server) -> None:
    url, bus, token = listing_server
    seen: dict[str, str] = {}

    def call() -> None:
        status, body = _get(url, token, "/v1/channels")
        seen["body"] = body
        assert status == 200, body

    statements = _channel_statements(bus, call)

    assert len(statements) == 3, statements
    assert sum("SELECT DISTINCT channel" in statement for statement in statements) == 1
    assert sum("FROM channels WHERE name IN" in statement for statement in statements) == 1
    assert sum("FROM channel_acl WHERE user_id" in statement for statement in statements) == 1
    assert not any("WHERE name = ?" in statement for statement in statements), statements
    assert set(json.loads(seen["body"])["channels"]) == set(VISIBLE)


def test_filter_runs_off_the_event_loop(listing_server) -> None:
    url, bus, token = listing_server
    original = bus.check_access_many
    observed: dict[str, bool] = {}

    def wrapper(channels, user_id, required="read"):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            observed["on_event_loop"] = False
        else:
            observed["on_event_loop"] = True
        return original(channels, user_id, required)

    bus.check_access_many = wrapper
    try:
        status, body = _get(url, token, "/v1/channels?detail=true")
        assert status == 200, body
    finally:
        bus.check_access_many = original

    assert observed.get("on_event_loop") is False


@pytest.mark.skipif(
    os.environ.get("MANSIO_PERF_TESTS") != "1",
    reason="timing regression; set MANSIO_PERF_TESTS=1 to run",
)
def test_listing_does_not_block_concurrent_requests(listing_server) -> None:
    url, bus, token = listing_server
    for i in range(600):
        bus.publish(f"mass-{i:04d}", OWNER, "chat", "x")

    def probe() -> list[float]:
        latencies = []
        for _ in range(8):
            start = time.perf_counter()
            _get(url, token, "/v1/presence")
            latencies.append((time.perf_counter() - start) * 1000)
        return latencies

    baseline = probe()
    during: list[float] = []
    box: dict[str, float] = {}

    def listing() -> None:
        start = time.perf_counter()
        _get(url, token, "/v1/channels?detail=true")
        box["ms"] = (time.perf_counter() - start) * 1000

    thread = threading.Thread(target=listing)
    thread.start()
    time.sleep(0.005)
    during.extend(probe())
    thread.join()

    baseline_p95 = sorted(baseline)[int(len(baseline) * 0.95) - 1]
    during_p95 = sorted(during)[int(len(during) * 0.95) - 1]
    assert during_p95 < max(20.0, baseline_p95 * 3), (baseline, during, box)
    assert statistics.median(during) < max(10.0, statistics.median(baseline) * 3)
