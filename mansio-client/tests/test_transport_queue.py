"""Queue claim/ack parsing keeps the server's lease_until (issue #359).

The memory backend sets ``lease_until`` on claim and the HTTP frontend
serializes it, but the transport rebuilt ``ClaimResult`` without the field.
"""

from __future__ import annotations

from mansio_client.transport import HttpTransport


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict | None = None) -> _FakeResponse:
        self.calls.append((url, json or {}))
        return _FakeResponse(self.payload)

    def close(self) -> None:
        pass


def _transport(payload: dict) -> HttpTransport:
    transport = HttpTransport("http://example.invalid", user_id="test-agent")
    real_http = transport._http
    transport._http = _FakeHttp(payload)
    real_http.close()
    return transport


def _message() -> dict:
    return {
        "id": "msg-1",
        "channel": "work",
        "sender": "user-a",
        "msg_type": "task",
        "payload": "do it",
        "timestamp": "2026-01-01T00:00:00Z",
    }


def _claim_payload(**overrides: object) -> dict:
    result: dict = {
        "message": _message(),
        "status": "claimed",
        "claimed_by": "user-a",
        "claimed_at": "2026-01-01T00:00:00Z",
    }
    result.update(overrides)
    return {"claimed": True, "result": result}


class TestQueueLeaseUntil:
    def test_claim_keeps_lease_until(self) -> None:
        transport = _transport(_claim_payload(lease_until="2026-01-01T00:05:00Z"))
        try:
            result = transport.queue_claim("work", "user-a", lease_seconds=300)
        finally:
            transport.close()
        assert result is not None
        assert result.lease_until == "2026-01-01T00:05:00Z"

    def test_claim_without_lease_until(self) -> None:
        transport = _transport(_claim_payload())
        try:
            result = transport.queue_claim("work", "user-a")
        finally:
            transport.close()
        assert result is not None
        assert result.lease_until is None

    def test_ack_keeps_lease_until(self) -> None:
        payload = {
            "acked": True,
            "result": {
                "message": _message(),
                "status": "completed",
                "claimed_by": "user-a",
                "claimed_at": "2026-01-01T00:00:00Z",
                "lease_until": "2026-01-01T00:05:00Z",
            },
        }
        transport = _transport(payload)
        try:
            result = transport.queue_ack("msg-1", "user-a")
        finally:
            transport.close()
        assert result is not None
        assert result.lease_until == "2026-01-01T00:05:00Z"
