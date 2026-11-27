"""Pin the HTTP route table and each module's contribution to it.

The ordered full table is the concatenation of the module contributions in
the composition order.  A module registered out of order, or one that
registers extra routes, changes the full table and fails the test.
"""

from __future__ import annotations

from typing import Any

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_core.authz import ChannelAuthorizer
from mansio_web._vendor.httpserver import App
from mansio_web.frontends.http import HttpFrontend
from mansio_web.frontends.routes import RouteContext
from mansio_web.frontends.routes import auth as auth_routes
from mansio_web.frontends.routes import channel_admin as channel_admin_routes
from mansio_web.frontends.routes import channels as channel_routes
from mansio_web.frontends.routes import health as health_routes
from mansio_web.frontends.routes import messages as message_routes
from mansio_web.frontends.routes import presence as presence_routes
from mansio_web.frontends.routes import publish as publish_routes
from mansio_web.frontends.routes import queue as queue_routes
from mansio_web.frontends.routes import registry as registry_routes
from mansio_web.frontends.routes import sse as sse_routes

Route = tuple[tuple[str, ...], str]

MODULE_ROUTES: list[tuple[Any, list[Route]]] = [
    (health_routes, [(("GET",), r"^/health$")]),
    (
        publish_routes,
        [
            (("POST",), r"^/v1/publish$"),
            (("GET",), r"^/v1/query$"),
        ],
    ),
    (channel_routes, [(("GET",), r"^/v1/channels$")]),
    (auth_routes, [(("GET",), r"^/v1/auth/check$")]),
    (
        message_routes,
        [
            (("DELETE",), r"^/v1/channels/([^/]+)$"),
            (("DELETE",), r"^/v1/messages/([^/]+)$"),
            (("POST",), r"^/v1/admin/channels/cleanup$"),
            (("POST",), r"^/v1/admin/compact$"),
        ],
    ),
    (registry_routes, [(("GET",), r"^/v1/registry/lookup$")]),
    (
        queue_routes,
        [
            (("POST",), r"^/v1/queue/claim$"),
            (("POST",), r"^/v1/queue/ack$"),
            (("GET",), r"^/v1/queue/status$"),
        ],
    ),
    (
        presence_routes,
        [
            (("POST",), r"^/v1/presence/heartbeat$"),
            (("GET",), r"^/v1/presence$"),
            (("GET",), r"^/v1/presence/([^/]+)$"),
        ],
    ),
    (
        channel_admin_routes,
        [
            (("POST",), r"^/v1/channels$"),
            (("GET",), r"^/v1/channels/([^/]+)/meta$"),
            (("GET",), r"^/v1/channels/([^/]+)/acl$"),
            (("PUT",), r"^/v1/channels/([^/]+)/acl$"),
            (("POST",), r"^/v1/channels/([^/]+)/acl$"),
            (("DELETE",), r"^/v1/channels/([^/]+)/acl/([^/]+)$"),
        ],
    ),
    (
        sse_routes,
        [
            (("GET",), r"^/v1/subscribe$"),
            (("GET",), r"^/v1/channels/([^/]+)/subscribe$"),
        ],
    ),
]


def _routes(app: App) -> list[Route]:
    return [(tuple(route.methods), route.pattern.pattern) for route in app._routes]


def _context(bus: Bus) -> RouteContext:
    return RouteContext(
        bus=bus,
        authz=ChannelAuthorizer(bus),
        token_store=None,
        allow_unauthenticated=True,
        max_query_limit=10_000,
        cors_origin="*",
        note_activity=lambda _user_id: None,
    )


def test_full_route_table_is_the_modules_in_order() -> None:
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(port=0, allow_unauthenticated=True)
    try:
        frontend.attach(bus)
        actual = _routes(frontend._app)
    finally:
        bus.close()

    expected = [route for _, routes in MODULE_ROUTES for route in routes]
    assert actual == expected
    assert len(actual) == 24


@pytest.mark.parametrize(
    ("module", "expected"),
    MODULE_ROUTES,
    ids=[module.__name__.rsplit(".", 1)[-1] for module, _ in MODULE_ROUTES],
)
def test_register_appends_only_its_own_routes(module: Any, expected: list[Route]) -> None:
    bus = Bus(backend=MemoryBackend())
    app = App()
    try:
        module.register(app, _context(bus))
        assert _routes(app) == expected
    finally:
        bus.close()
