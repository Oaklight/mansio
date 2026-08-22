"""Thread safety tests for MemoryBackend.

Exercises concurrent reads and writes across all MemoryBackend operations
to verify that the internal locking is correct and no data corruption occurs.

Closes #44.
"""

from __future__ import annotations

import threading
import time

from piazza import Bus, MemoryBackend


def _make_backend() -> MemoryBackend:
    return MemoryBackend()


def _make_bus(backend: MemoryBackend | None = None) -> Bus:
    return Bus(backend=backend or _make_backend())


class TestConcurrentStoreAndQuery:
    """Concurrent publish + query must not corrupt state or raise."""

    def test_concurrent_publish_same_channel(self):
        bus = _make_bus()
        n_per_thread = 200
        n_threads = 4

        def worker(thread_id: int):
            for i in range(n_per_thread):
                bus.publish("shared", f"agent-{thread_id}", "text", f"t{thread_id}-{i}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        msgs = bus.poll("shared", limit=10_000)
        assert len(msgs) == n_per_thread * n_threads

    def test_concurrent_publish_different_channels(self):
        bus = _make_bus()
        n_per_thread = 200
        n_threads = 4

        def worker(thread_id: int):
            ch = f"ch-{thread_id}"
            for i in range(n_per_thread):
                bus.publish(ch, "agent", "text", f"msg-{i}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        channels = bus.channels()
        assert len(channels) == n_threads
        for t in range(n_threads):
            assert len(bus.poll(f"ch-{t}", limit=10_000)) == n_per_thread

    def test_concurrent_publish_and_query(self):
        """Readers must not see partial or corrupted state."""
        bus = _make_bus()
        errors: list[str] = []
        stop = threading.Event()

        def writer():
            for i in range(500):
                bus.publish("data", "writer", "text", f"msg-{i}")
            stop.set()

        def reader():
            while not stop.is_set():
                msgs = bus.poll("data", limit=10_000)
                ids = [m.id for m in msgs]
                if len(ids) != len(set(ids)):
                    errors.append("duplicate message ids in poll result")
                    return

        writer_t = threading.Thread(target=writer)
        readers = [threading.Thread(target=reader) for _ in range(3)]
        for t in readers:
            t.start()
        writer_t.start()
        writer_t.join()
        for t in readers:
            t.join()

        assert not errors
        assert len(bus.poll("data", limit=10_000)) == 500


class TestConcurrentBackendQueryAll:
    """query_all on the backend with concurrent writes must remain consistent."""

    def test_concurrent_publish_and_query_all(self):
        backend = _make_backend()
        bus = _make_bus(backend)
        stop = threading.Event()
        errors: list[str] = []

        def writer():
            for i in range(300):
                bus.publish(f"ch-{i % 5}", "writer", "text", f"msg-{i}")
            stop.set()

        def reader():
            while not stop.is_set():
                msgs = backend.query_all(limit=10_000)
                ids = [m.id for m in msgs]
                if len(ids) != len(set(ids)):
                    errors.append("duplicate ids in query_all")
                    return

        writer_t = threading.Thread(target=writer)
        readers = [threading.Thread(target=reader) for _ in range(3)]
        for t in readers:
            t.start()
        writer_t.start()
        writer_t.join()
        for t in readers:
            t.join()

        assert not errors


class TestConcurrentCountAndList:
    """channels / count_messages under concurrent writes."""

    def test_concurrent_publish_and_list_channels(self):
        bus = _make_bus()
        stop = threading.Event()
        errors: list[str] = []

        def writer():
            for i in range(200):
                bus.publish(f"ch-{i % 10}", "agent", "text", f"msg-{i}")
            stop.set()

        def lister():
            while not stop.is_set():
                channels = bus.channels()
                if not isinstance(channels, list):
                    errors.append("channels did not return a list")
                    return

        writer_t = threading.Thread(target=writer)
        lister_t = threading.Thread(target=lister)
        writer_t.start()
        lister_t.start()
        writer_t.join()
        lister_t.join()

        assert not errors

    def test_concurrent_publish_and_count(self):
        backend = _make_backend()
        bus = _make_bus(backend)
        stop = threading.Event()
        errors: list[str] = []

        def writer():
            for i in range(200):
                bus.publish("ch", "agent", "text", f"msg-{i}")
            stop.set()

        def counter():
            prev = 0
            while not stop.is_set():
                n = backend.count_messages("ch")
                if n < prev:
                    errors.append(f"count decreased: {prev} → {n}")
                    return
                prev = n

        writer_t = threading.Thread(target=writer)
        counter_t = threading.Thread(target=counter)
        writer_t.start()
        counter_t.start()
        writer_t.join()
        counter_t.join()

        assert not errors
        assert backend.count_messages("ch") == 200


class TestConcurrentStats:
    """get_stats must not crash under concurrent writes."""

    def test_concurrent_publish_and_stats(self):
        backend = _make_backend()
        bus = _make_bus(backend)
        stop = threading.Event()
        errors: list[str] = []

        def writer():
            for i in range(300):
                bus.publish(f"ch-{i % 5}", f"agent-{i % 3}", "text", f"msg-{i}")
            stop.set()

        def stats_reader():
            while not stop.is_set():
                try:
                    stats = backend.get_stats()
                    assert "total_messages" in stats
                except Exception as exc:
                    errors.append(str(exc))
                    return

        writer_t = threading.Thread(target=writer)
        reader_t = threading.Thread(target=stats_reader)
        writer_t.start()
        reader_t.start()
        writer_t.join()
        reader_t.join()

        assert not errors


class TestConcurrentPresence:
    """Heartbeat and agent listing under concurrent access."""

    def test_concurrent_heartbeats(self):
        bus = _make_bus()
        n_agents = 8
        n_heartbeats = 100

        def heartbeat_worker(agent_id: str):
            for i in range(n_heartbeats):
                bus.heartbeat(agent_id, metadata={"i": i})

        threads = [
            threading.Thread(target=heartbeat_worker, args=(f"agent-{a}",)) for a in range(n_agents)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        agents = bus.agents()
        assert len(agents) == n_agents

    def test_concurrent_heartbeat_and_agents_list(self):
        bus = _make_bus()
        stop = threading.Event()
        errors: list[str] = []

        def heartbeat_writer():
            for i in range(200):
                bus.heartbeat(f"agent-{i % 10}", metadata={"round": i})
            stop.set()

        def agents_reader():
            while not stop.is_set():
                try:
                    agents = bus.agents()
                    for a in agents:
                        assert a.agent_id is not None
                        assert a.status in ("online", "offline")
                except Exception as exc:
                    errors.append(str(exc))
                    return

        writer_t = threading.Thread(target=heartbeat_writer)
        reader_t = threading.Thread(target=agents_reader)
        writer_t.start()
        reader_t.start()
        writer_t.join()
        reader_t.join()

        assert not errors


class TestConcurrentQueueOps:
    """Queue claim/ack under concurrent access — each message claimed exactly once."""

    def test_concurrent_claim_no_double_claim(self):
        bus = _make_bus()
        n_messages = 50
        for i in range(n_messages):
            bus.publish("jobs", "producer", "task", f"task-{i}", queue=True)

        claimed: list[str] = []
        lock = threading.Lock()

        def claimer(worker_id: str):
            while True:
                result = bus.claim("jobs", worker_id)
                if result is None:
                    break
                with lock:
                    claimed.append(result.message.id)

        threads = [threading.Thread(target=claimer, args=(f"w-{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(claimed) == n_messages
        assert len(set(claimed)) == n_messages, "double-claim detected"


class TestConcurrentRetire:
    """retire_completed under concurrent publish/claim/ack."""

    def test_retire_during_publish(self):
        backend = _make_backend()
        bus = _make_bus(backend)
        errors: list[str] = []

        # Pre-populate with completed queue items.
        for i in range(20):
            bus.publish("q", "p", "task", f"old-{i}", queue=True)
        for _ in range(20):
            r = bus.claim("q", "worker")
            if r:
                bus.ack(r.message.id, "worker")

        stop = threading.Event()

        def publisher():
            for i in range(100):
                bus.publish("q", "p", "task", f"new-{i}", queue=True)
            stop.set()

        def retirer():
            while not stop.is_set():
                try:
                    backend.retire_completed(max_age_seconds=0)
                except Exception as exc:
                    errors.append(str(exc))
                    return

        pub_t = threading.Thread(target=publisher)
        ret_t = threading.Thread(target=retirer)
        pub_t.start()
        ret_t.start()
        pub_t.join()
        ret_t.join()

        assert not errors


class TestConcurrentMixedOps:
    """Stress test: all operation types running concurrently."""

    def test_all_ops_concurrent(self):
        backend = _make_backend()
        bus = _make_bus(backend)
        errors: list[str] = []
        stop = threading.Event()

        def publisher():
            for i in range(200):
                bus.publish(f"ch-{i % 3}", f"agent-{i % 4}", "text", f"msg-{i}")
            stop.set()

        def querier():
            while not stop.is_set():
                try:
                    bus.poll("ch-0", limit=100)
                    backend.query_all(limit=100)
                    bus.channels()
                    backend.count_messages()
                except Exception as exc:
                    errors.append(f"querier: {exc}")
                    return

        def stats_checker():
            while not stop.is_set():
                try:
                    backend.get_stats()
                    backend.get_backend_info()
                    backend.query_recent_timestamps(seconds=10)
                except Exception as exc:
                    errors.append(f"stats: {exc}")
                    return

        def presence_worker():
            for i in range(100):
                bus.heartbeat(f"agent-{i % 5}")
            while not stop.is_set():
                try:
                    bus.agents()
                    bus.agent_status("agent-0")
                except Exception as exc:
                    errors.append(f"presence: {exc}")
                    return
                time.sleep(0.001)

        threads = [
            threading.Thread(target=publisher),
            threading.Thread(target=querier),
            threading.Thread(target=querier),
            threading.Thread(target=stats_checker),
            threading.Thread(target=presence_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert backend.count_messages() == 200
