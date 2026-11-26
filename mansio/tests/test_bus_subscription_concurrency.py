"""Concurrency regression tests for the Bus subscription registry (issue #335).

``Bus.unsubscribe()`` and ``Bus.subscription_counts()`` used to iterate
``self._subs.items()`` while another thread's ``subscribe()`` inserted a new
channel key, raising ``RuntimeError: dictionary changed size during
iteration``.  The first-subscribe path could also create duplicate backend
watches for one channel.
"""

from __future__ import annotations

import threading
import time

from mansio import Bus, MemoryBackend
from mansio.types import Message


def _join_all(threads: list[threading.Thread], timeout: float = 5.0) -> None:
    for thread in threads:
        thread.join(timeout=timeout)


class TestSubscriptionChurn:
    def test_churn_with_counts_does_not_raise(self) -> None:
        """Subscribing to new channels while counting subscriptions must not crash."""
        bus = Bus(backend=MemoryBackend())
        stop = threading.Event()
        errors: list[BaseException] = []

        def churn(worker: int) -> None:
            i = 0
            try:
                while not stop.is_set():
                    sub_id = bus.subscribe(f"ch-{worker}-{i}", lambda _m: None)
                    if i % 2:
                        bus.unsubscribe(sub_id)
                    i += 1
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted
                errors.append(exc)
                stop.set()

        def count() -> None:
            try:
                while not stop.is_set():
                    assert isinstance(bus.subscription_counts(), dict)
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted
                errors.append(exc)
                stop.set()

        def publish() -> None:
            try:
                while not stop.is_set():
                    bus.publish("hot", "agent", "text", "tick")
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted
                errors.append(exc)
                stop.set()

        threads = [threading.Thread(target=churn, args=(i,)) for i in range(4)]
        threads += [threading.Thread(target=count), threading.Thread(target=publish)]
        for thread in threads:
            thread.start()
        time.sleep(1.0)
        stop.set()
        _join_all(threads)
        bus.close()

        assert errors == []

    def test_callback_may_unsubscribe_during_delivery(self) -> None:
        """Dispatch snapshots callbacks, so a callback can unsubscribe itself."""
        bus = Bus(backend=MemoryBackend())
        received: list[Message] = []
        holder: dict[str, str] = {}

        def self_removing(message: Message) -> None:
            bus.unsubscribe(holder["sid"])

        holder["sid"] = bus.subscribe("ch", self_removing)
        bus.subscribe("ch", received.append)

        msg_id = bus.publish("ch", "agent", "text", "hello")

        assert [m.id for m in received] == [msg_id]
        assert bus.subscription_counts()["ch"] != [holder["sid"]]
        bus.close()


class _SlowWatchBackend(MemoryBackend):
    """MemoryBackend whose watch() blocks, widening the first-subscribe race."""

    def __init__(self) -> None:
        super().__init__()
        self.watch_calls = 0
        self._count_lock = threading.Lock()

    def watch(self, channel: str, callback) -> str:
        time.sleep(0.02)
        with self._count_lock:
            self.watch_calls += 1
            return f"w{self.watch_calls}"

    def unwatch(self, watch_id: str) -> None:
        pass


class TestFirstSubscribeWatch:
    def test_concurrent_first_subscribe_creates_one_watch(self) -> None:
        """One watch per channel even when many threads subscribe at once."""
        backend = _SlowWatchBackend()
        bus = Bus(backend=backend)
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            bus.subscribe("ch", lambda _m: None)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        _join_all(threads)

        assert backend.watch_calls == 1
        assert len(bus.subscription_counts()["ch"]) == 8
        bus.close()
