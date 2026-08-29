"""Tests for input validation in admin endpoints (#221).

Covers:
- GET /api/messages?channel=x&limit=abc → 400 (non-integer)
- GET /api/messages?channel=x&limit=-1 → 400 (negative)
- GET /api/messages?channel=x&limit=0 → 400 (zero)
"""

from __future__ import annotations

import pytest

from mansio import Bus, MemoryBackend
from mansio._vendor.httpclient import Client as HttpClient
from mansio.admin.server import AdminServer


@pytest.fixture()
def admin_url():
    """Start AdminServer without auth, yield URL."""
    bus = Bus(backend=MemoryBackend())
    admin = AdminServer(bus, port=0)
    info = admin.start()
    yield info.url
    admin.stop()
    bus.close()


class TestAdminMessagesLimitValidation:
    """Malformed limit on admin /api/messages should return 400."""

    @pytest.mark.parametrize(
        "limit_val",
        ["abc", "notanumber", "3.5", "null"],
        ids=["alpha", "word", "float", "null-string"],
    )
    def test_bad_limit_rejected(self, admin_url: str, limit_val: str) -> None:
        http = HttpClient()
        resp = http.get(f"{admin_url}/api/messages?channel=test&limit={limit_val}")
        assert resp.status_code == 400, (
            f"GET /api/messages?limit={limit_val!r}: expected 400, got {resp.status_code}"
        )
        http.close()

    def test_negative_limit_rejected(self, admin_url: str) -> None:
        http = HttpClient()
        resp = http.get(f"{admin_url}/api/messages?channel=test&limit=-1")
        assert resp.status_code == 400, "GET /api/messages?limit=-1: expected 400, got " + str(
            resp.status_code
        )
        http.close()

    def test_zero_limit_rejected(self, admin_url: str) -> None:
        http = HttpClient()
        resp = http.get(f"{admin_url}/api/messages?channel=test&limit=0")
        assert resp.status_code == 400, "GET /api/messages?limit=0: expected 400, got " + str(
            resp.status_code
        )
        http.close()

    def test_valid_limit_accepted(self, admin_url: str) -> None:
        http = HttpClient()
        resp = http.get(f"{admin_url}/api/messages?channel=test&limit=10")
        assert resp.status_code == 200, (
            f"GET /api/messages?limit=10: expected 200, got {resp.status_code}"
        )
        http.close()

    def test_missing_channel_rejected(self, admin_url: str) -> None:
        http = HttpClient()
        resp = http.get(f"{admin_url}/api/messages")
        assert resp.status_code == 400, (
            f"GET /api/messages (no channel): expected 400, got {resp.status_code}"
        )
        http.close()
