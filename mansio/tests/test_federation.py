"""Tests for federation — FederationLink cross-instance communication.

Verifies:
- Pull replication: messages flow remote → local
- Push replication: messages flow local → remote
- Bidirectional replication: messages flow both ways
- Anti-loop: bridged messages are not re-bridged (no infinite loops)
- stop_replication: messages stop flowing after stop
- route_read / route_send: on-demand proxy to remote instance
- close() cleans up all subscriptions
- replicating property reports active channels
- Invalid mode raises ValueError
- Duplicate replication raises ValueError
"""

from __future__ import annotations

import threading
import time

import pytest
from mansio_client.federation import FederationLink

from mansio import Bus, MansioClient, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend


def _start_server():
    """Start a MansioServer on a random port, return (url, server)."""
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    url = f"http://{host}:{port}"
    return url, server


@pytest.fixture()
def two_servers():
    """Start two independent MansioServer instances, yield (url_a, url_b)."""
    url_a, server_a = _start_server()
    url_b, server_b = _start_server()

    yield url_a, url_b

    server_a.shutdown()
    server_b.shutdown()


@pytest.fixture()
def linked(two_servers):
    """Create two clients and a FederationLink, yield (link, client_a, client_b)."""
    url_a, url_b = two_servers
    client_a = MansioClient(url_a, "agent-a")
    client_b = MansioClient(url_b, "agent-b")

    link = FederationLink(
        client_a,
        client_b,
        local_instance="instance-a",
        remote_instance="instance-b",
    )

    yield link, client_a, client_b

    link.close()
    client_a.close()
    client_b.close()


# ── Replication Tests ─────────────────────────────────────────


class TestPullReplication:
    """Pull mode: remote → local."""

    def test_message_flows_remote_to_local(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["shared"], mode="pull")
        time.sleep(0.5)  # let SSE connect

        # Publish on remote (instance-b)
        client_b.channel_send("shared", "hello from b")
        time.sleep(1.0)  # let SSE deliver + bridge

        # Should appear on local (instance-a)
        msgs = client_a.channel_read("shared", limit=10)
        bridged = [m for m in msgs if m.payload == "hello from b"]
        assert len(bridged) == 1
        assert bridged[0].metadata["bridged"] is True
        assert bridged[0].metadata["source_instance"] == "instance-b"
        assert bridged[0].metadata["original_sender"] == "agent-b"

    def test_local_message_does_not_flow_to_remote(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["shared"], mode="pull")
        time.sleep(0.5)

        # Publish on local (instance-a)
        client_a.channel_send("shared", "local only")
        time.sleep(1.0)

        # Should NOT appear on remote (instance-b)
        msgs = client_b.channel_read("shared", limit=10)
        bridged = [m for m in msgs if m.payload == "local only"]
        assert len(bridged) == 0


class TestPushReplication:
    """Push mode: local → remote."""

    def test_message_flows_local_to_remote(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["shared"], mode="push")
        time.sleep(0.5)

        # Publish on local (instance-a)
        client_a.channel_send("shared", "hello from a")
        time.sleep(1.0)

        # Should appear on remote (instance-b)
        msgs = client_b.channel_read("shared", limit=10)
        bridged = [m for m in msgs if m.payload == "hello from a"]
        assert len(bridged) == 1
        assert bridged[0].metadata["bridged"] is True
        assert bridged[0].metadata["source_instance"] == "instance-a"

    def test_remote_message_does_not_flow_to_local(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["shared"], mode="push")
        time.sleep(0.5)

        # Publish on remote (instance-b)
        client_b.channel_send("shared", "remote only")
        time.sleep(1.0)

        # Should NOT appear on local (instance-a)
        msgs = client_a.channel_read("shared", limit=10)
        bridged = [m for m in msgs if m.payload == "remote only"]
        assert len(bridged) == 0


class TestBidirectionalReplication:
    """Bidirectional mode: both directions."""

    def test_messages_flow_both_ways(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["shared"], mode="bidirectional")
        time.sleep(0.5)

        # Publish on remote → appears on local
        client_b.channel_send("shared", "from b")
        time.sleep(1.0)

        msgs_a = client_a.channel_read("shared", limit=10)
        assert any(m.payload == "from b" for m in msgs_a)

        # Publish on local → appears on remote
        client_a.channel_send("shared", "from a")
        time.sleep(1.0)

        msgs_b = client_b.channel_read("shared", limit=10)
        assert any(m.payload == "from a" for m in msgs_b)


# ── Anti-loop Tests ───────────────────────────────────────────


class TestAntiLoop:
    """Bridged messages must not be re-bridged."""

    def test_no_infinite_loop_bidirectional(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["echo-test"], mode="bidirectional")
        time.sleep(0.5)

        # Send one message from b
        client_b.channel_send("echo-test", "ping")
        time.sleep(2.0)  # generous wait to catch any loops

        # On instance-a: should have exactly 1 bridged copy
        msgs_a = client_a.channel_read("echo-test", limit=100)
        pings_a = [m for m in msgs_a if m.payload == "ping"]
        assert len(pings_a) == 1
        assert pings_a[0].metadata["bridged"] is True

        # On instance-b: should have exactly 1 original (not re-bridged back)
        msgs_b = client_b.channel_read("echo-test", limit=100)
        pings_b = [m for m in msgs_b if m.payload == "ping"]
        assert len(pings_b) == 1
        # The original should NOT have bridged metadata
        assert pings_b[0].metadata is None or not pings_b[0].metadata.get("bridged")

    def test_bridged_metadata_skipped(self, linked) -> None:
        """Manually publishing a message with bridged=True should not be forwarded."""
        link, client_a, client_b = linked
        link.replicate(["meta-test"], mode="pull")
        time.sleep(0.5)

        # Publish an already-bridged message on remote
        client_b.channel_send(
            "meta-test",
            "already bridged",
            metadata={"bridged": True, "source_instance": "elsewhere"},
        )
        time.sleep(1.0)

        # Should NOT appear on local
        msgs_a = client_a.channel_read("meta-test", limit=10)
        bridged = [m for m in msgs_a if m.payload == "already bridged"]
        assert len(bridged) == 0


# ── Stop / Lifecycle Tests ────────────────────────────────────


class TestStopReplication:
    """Stopping replication halts message flow."""

    def test_stop_halts_flow(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["stoptest"], mode="pull")
        time.sleep(0.5)

        # Message flows before stop
        client_b.channel_send("stoptest", "before stop")
        time.sleep(1.0)
        msgs = client_a.channel_read("stoptest", limit=10)
        assert any(m.payload == "before stop" for m in msgs)

        # Stop replication
        link.stop_replication(["stoptest"])
        time.sleep(0.3)

        # Message after stop should NOT flow
        client_b.channel_send("stoptest", "after stop")
        time.sleep(1.0)
        msgs = client_a.channel_read("stoptest", limit=10)
        assert not any(m.payload == "after stop" for m in msgs)

    def test_stop_all(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["ch1", "ch2"], mode="pull")
        assert len(link.replicating) == 2

        link.stop_replication()
        assert len(link.replicating) == 0

    def test_close_stops_all(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["ch1"], mode="pull")
        assert len(link.replicating) == 1

        link.close()
        assert len(link.replicating) == 0


# ── Routing Tests ─────────────────────────────────────────────


class TestRouting:
    """On-demand federated routing (no replication)."""

    def test_route_read(self, linked) -> None:
        link, client_a, client_b = linked

        # Publish directly on remote
        client_b.channel_send("remote-only", "secret data")

        # Read via routing (no local copy)
        msgs = link.route_read("remote-only", limit=10)
        assert len(msgs) >= 1
        assert any(m.payload == "secret data" for m in msgs)

        # Should NOT be on local
        local_msgs = client_a.channel_read("remote-only", limit=10)
        assert not any(m.payload == "secret data" for m in local_msgs)

    def test_route_send(self, linked) -> None:
        link, client_a, client_b = linked

        # Send via routing to remote
        msg_id = link.route_send("remote-ch", "routed message")
        assert msg_id

        # Should appear on remote
        msgs = client_b.channel_read("remote-ch", limit=10)
        assert any(m.payload == "routed message" for m in msgs)


# ── Property / Validation Tests ───────────────────────────────


class TestProperties:
    """Properties and validation."""

    def test_replicating_property(self, linked) -> None:
        link, client_a, client_b = linked
        assert link.replicating == {}

        link.replicate(["ch-x"], mode="pull")
        assert link.replicating == {"ch-x": "pull"}

        link.replicate(["ch-y"], mode="push")
        assert link.replicating == {"ch-x": "pull", "ch-y": "push"}

    def test_invalid_mode_raises(self, linked) -> None:
        link, client_a, client_b = linked
        with pytest.raises(ValueError, match="Invalid replication mode"):
            link.replicate(["ch"], mode="invalid")

    def test_duplicate_replication_raises(self, linked) -> None:
        link, client_a, client_b = linked
        link.replicate(["dup-ch"], mode="pull")
        with pytest.raises(ValueError, match="already being replicated"):
            link.replicate(["dup-ch"], mode="push")

    def test_self_loop_raises(self, two_servers) -> None:
        url_a, url_b = two_servers
        client_a = MansioClient(url_a, "loop-a")
        client_b = MansioClient(url_b, "loop-b")
        with pytest.raises(ValueError, match="must differ"):
            FederationLink(
                client_a,
                client_b,
                local_instance="same",
                remote_instance="same",
            )
        client_a.close()
        client_b.close()

    def test_instance_ids(self, linked) -> None:
        link, client_a, client_b = linked
        assert link.local_instance == "instance-a"
        assert link.remote_instance == "instance-b"

    def test_repr(self, linked) -> None:
        link, client_a, client_b = linked
        r = repr(link)
        assert "instance-a" in r
        assert "instance-b" in r
        assert "replicating=0" in r

    def test_context_manager(self, two_servers) -> None:
        url_a, url_b = two_servers
        client_a = MansioClient(url_a, "ctx-a")
        client_b = MansioClient(url_b, "ctx-b")

        with FederationLink(client_a, client_b) as link:
            link.replicate(["ctx-ch"], mode="pull")
            assert len(link.replicating) == 1

        # After exit, replication should be stopped
        assert len(link.replicating) == 0

        client_a.close()
        client_b.close()
