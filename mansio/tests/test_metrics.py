"""Tests for the in-process metrics collector."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from mansio import Bus, MemoryBackend
from mansio.admin.metrics import MetricsCollector, _RollingWindow

# ──────────────────────────────────────────────
# _RollingWindow
# ──────────────────────────────────────────────


class TestRollingWindow:
    """Core rolling window data structure."""

    def test_empty_series(self):
        w = _RollingWindow(window=10)
        series = w.get_series(5)
        assert series == [0, 0, 0, 0, 0]

    def test_record_increments_current_bucket(self):
        w = _RollingWindow(window=10)
        w.record()
        w.record()
        w.record()
        series = w.get_series(1)
        assert series == [3]

    def test_total_counts_all_active(self):
        w = _RollingWindow(window=10)
        w.record(5)
        w.record(3)
        assert w.total == 8

    def test_series_length_matches_requested(self):
        w = _RollingWindow(window=60)
        w.record()
        series = w.get_series(30)
        assert len(series) == 30

    def test_series_capped_at_window(self):
        w = _RollingWindow(window=10)
        series = w.get_series(100)
        assert len(series) == 10

    def test_default_series_returns_full_window(self):
        w = _RollingWindow(window=5)
        series = w.get_series()
        assert len(series) == 5

    def test_expired_buckets_cleaned_on_read(self):
        w = _RollingWindow(window=2)
        # Manually insert an old bucket
        old_key = int(time.monotonic()) - 10
        with w._lock:
            w._buckets[old_key] = 99
        # Read should clean it
        w.get_series(1)
        with w._lock:
            assert old_key not in w._buckets

    def test_total_ignores_expired(self):
        w = _RollingWindow(window=2)
        old_key = int(time.monotonic()) - 10
        with w._lock:
            w._buckets[old_key] = 99
        assert w.total == 0

    def test_record_different_seconds(self):
        w = _RollingWindow(window=60)
        # Mock monotonic to control bucket placement
        base = 1000.0
        with patch("mansio.admin.metrics.time") as mock_time:
            mock_time.monotonic.return_value = base
            w.record(2)
            mock_time.monotonic.return_value = base + 1
            w.record(3)
            mock_time.monotonic.return_value = base + 2
            w.record(1)

            # Get series — last 3 seconds
            series = w.get_series(3)
        assert series == [2, 3, 1]

    def test_thread_safety(self):
        """Multiple threads recording concurrently."""
        w = _RollingWindow(window=10)
        errors: list[Exception] = []

        def writer():
            try:
                for _ in range(1000):
                    w.record()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert w.total == 4000


# ──────────────────────────────────────────────
# MetricsCollector
# ──────────────────────────────────────────────


class TestMetricsCollector:
    """High-level collector used by Bus."""

    def test_record_and_get_throughput(self):
        mc = MetricsCollector(window=60)
        mc.record()
        mc.record()
        mc.record()
        buckets = mc.get_throughput(seconds=1)
        assert len(buckets) == 1
        assert buckets[0]["count"] == 3
        assert "time" in buckets[0]

    def test_throughput_shape(self):
        mc = MetricsCollector(window=60)
        mc.record()
        buckets = mc.get_throughput(seconds=5)
        assert len(buckets) == 5
        for b in buckets:
            assert "time" in b
            assert "count" in b
            assert isinstance(b["time"], str)
            assert isinstance(b["count"], int)

    def test_total_messages(self):
        mc = MetricsCollector(window=60)
        assert mc.total_messages == 0
        mc.record()
        mc.record()
        assert mc.total_messages == 2

    def test_throughput_time_format(self):
        mc = MetricsCollector(window=10)
        mc.record()
        buckets = mc.get_throughput(seconds=1)
        # Should be HH:MM:SS format
        t = str(buckets[0]["time"])
        parts = t.split(":")
        assert len(parts) == 3
        assert all(len(p) == 2 for p in parts)


# ──────────────────────────────────────────────
# Bus Integration
# ──────────────────────────────────────────────


class TestBusMetricsIntegration:
    """Verify metrics are wired into Bus.publish()."""

    @pytest.fixture()
    def bus(self):
        return Bus(backend=MemoryBackend())

    def test_bus_has_metrics(self, bus):
        assert isinstance(bus.metrics, MetricsCollector)

    def test_publish_increments_metrics(self, bus):
        assert bus.metrics.total_messages == 0
        bus.publish("ch", "alice", "text", "hello")
        assert bus.metrics.total_messages == 1
        bus.publish("ch", "alice", "text", "world")
        assert bus.metrics.total_messages == 2

    def test_publish_queue_increments_metrics(self, bus):
        bus.publish("q", "alice", "text", "task", queue=True)
        assert bus.metrics.total_messages == 1

    def test_metrics_throughput_after_publish(self, bus):
        for _ in range(5):
            bus.publish("ch", "alice", "text", "msg")
        buckets = bus.metrics.get_throughput(seconds=1)
        assert buckets[0]["count"] == 5
