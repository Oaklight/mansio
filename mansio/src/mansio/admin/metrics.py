"""In-process metrics collector with rolling time-series windows.

Provides pre-aggregated per-second message counts using
``time.monotonic()`` bucket keys, avoiding O(n) backend queries
on every dashboard refresh.

Thread-safe: ``record()`` is called from ``Bus.publish()`` which
may run on multiple threads.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone


class _RollingWindow:
    """Fixed-size rolling window of per-second counters.

    Bucket keys are integer seconds from ``time.monotonic()``.
    Expired buckets are lazily cleaned on read, never on the hot path.

    Args:
        window: Number of seconds to retain (default 300).
    """

    __slots__ = ("_window", "_buckets", "_lock")

    def __init__(self, window: int = 300) -> None:
        self._window = window
        self._buckets: dict[int, int] = {}
        self._lock = threading.Lock()

    def record(self, count: int = 1) -> None:
        """Increment the current second's counter."""
        key = int(time.monotonic())
        with self._lock:
            self._buckets[key] = self._buckets.get(key, 0) + count
            if len(self._buckets) > self._window * 2:
                cutoff = key - self._window
                self._buckets = {k: v for k, v in self._buckets.items() if k > cutoff}

    def get_series(self, seconds: int | None = None) -> list[int]:
        """Return per-second counts for the last *seconds* seconds.

        Args:
            seconds: Number of seconds to return (default: full window).

        Returns:
            List of length *seconds*, oldest first. Each element is the
            message count for that one-second bucket.
        """
        if seconds is None:
            seconds = self._window
        seconds = min(seconds, self._window)

        now = int(time.monotonic())
        cutoff = now - self._window

        with self._lock:
            # Lazy cleanup of expired buckets
            expired = [k for k in self._buckets if k <= cutoff]
            for k in expired:
                del self._buckets[k]

            return [self._buckets.get(now - seconds + 1 + i, 0) for i in range(seconds)]

    @property
    def total(self) -> int:
        """Total count across all active buckets."""
        now = int(time.monotonic())
        cutoff = now - self._window
        with self._lock:
            return sum(v for k, v in self._buckets.items() if k > cutoff)


class MetricsCollector:
    """Collects publish metrics for the admin dashboard.

    Usage::

        collector = MetricsCollector()
        # On every Bus.publish():
        collector.record()
        # On dashboard throughput request:
        series = collector.get_throughput(seconds=60)

    Args:
        window: Rolling window size in seconds (default 300).
    """

    def __init__(self, window: int = 300) -> None:
        self._throughput = _RollingWindow(window)

    def record(self) -> None:
        """Record a single published message."""
        self._throughput.record()

    def get_throughput(self, seconds: int = 60) -> list[dict[str, object]]:
        """Return throughput buckets matching the existing API shape.

        Args:
            seconds: Number of seconds to return.

        Returns:
            List of ``{"time": "HH:MM:SS", "count": N}`` dicts,
            oldest first, same shape as the existing endpoint.
        """

        series = self._throughput.get_series(seconds)
        now = datetime.now(timezone.utc)
        buckets: list[dict[str, object]] = []
        for i, count in enumerate(series):
            t = now - timedelta(seconds=seconds - 1 - i)
            buckets.append({"time": t.strftime("%H:%M:%S"), "count": count})
        return buckets

    @property
    def total_messages(self) -> int:
        """Total messages recorded in the rolling window."""
        return self._throughput.total
