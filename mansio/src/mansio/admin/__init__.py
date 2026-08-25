"""Admin panel for mansio message bus.

Provides an HTTP admin server with REST API and web UI for
monitoring channels, messages, subscriptions, and throughput.
"""

from .metrics import MetricsCollector
from .server import AdminInfo, AdminServer

__all__ = ["AdminInfo", "AdminServer", "MetricsCollector"]
