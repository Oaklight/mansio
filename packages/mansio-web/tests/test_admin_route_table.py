"""Pin the admin route table: exact (method, path pattern) set and order."""

from __future__ import annotations

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_web.admin.server import AdminServer

SERVE_UI = [
    (("GET",), r"^/assets/(.+)$"),
    (("GET",), r"^/$"),
    (("GET",), r"^/api/auth\-check$"),
    (("POST",), r"^/api/login$"),
    (("POST",), r"^/api/logout$"),
    (("GET",), r"^/api/stats$"),
    (("GET",), r"^/api/stats/throughput$"),
    (("GET",), r"^/api/channels$"),
    (("GET",), r"^/api/channels/([^/]+)$"),
    (("GET",), r"^/api/messages$"),
    (("POST",), r"^/api/messages$"),
    (("GET",), r"^/api/subscriptions$"),
    (("GET",), r"^/api/system$"),
    (("GET",), r"^/api/prefixes$"),
    (("POST",), r"^/api/prefixes$"),
    (("DELETE",), r"^/api/prefixes/([^/]+)$"),
    (("GET",), r"^/api/users$"),
    (("POST",), r"^/api/users$"),
    (("GET",), r"^/api/users/([^/]+)$"),
    (("DELETE",), r"^/api/users/([^/]+)$"),
    (("GET",), r"^/api/users/([^/]+)/tokens$"),
    (("POST",), r"^/api/users/([^/]+)/tokens$"),
    (("GET",), r"^/api/tokens$"),
    (("POST",), r"^/api/tokens$"),
    (("DELETE",), r"^/api/tokens/([^/]+)$"),
    (("POST",), r"^/api/tokens/([^/]+)/rotate$"),
]


@pytest.mark.parametrize("serve_ui", [True, False])
def test_route_table_is_unchanged(serve_ui: bool) -> None:
    bus = Bus(backend=MemoryBackend())
    admin = AdminServer(bus, port=0, serve_ui=serve_ui)
    actual = [(tuple(route.methods), route.pattern.pattern) for route in admin._app._routes]
    bus.close()
    assert actual == SERVE_UI
