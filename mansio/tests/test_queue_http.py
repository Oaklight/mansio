"""Tests for queue HTTP endpoints (/v1/queue/*).

Covers:
- GET  /v1/queue/status — queue status of a message (issue #145)
- POST /v1/queue/claim  — canonical claim path
- POST /v1/queue/ack    — canonical ack path
- POST /v1/claim        — deprecated alias (backward compat)
- POST /v1/ack          — deprecated alias (backward compat)
"""

from __future__ import annotations

from mansio._vendor.httpclient import Client as HttpClient


def _publish_queue_msg(http: HttpClient, url: str, channel: str = "jobs") -> str:
    """Publish a queue message and return the message_id."""
    resp = http.post(
        f"{url}/v1/publish",
        json={
            "channel": channel,
            "sender": "admin",
            "msg_type": "task",
            "payload": "do stuff",
            "queue": True,
        },
    )
    assert resp.status_code == 200
    return resp.json()["message_id"]


def _publish_regular_msg(http: HttpClient, url: str, channel: str = "chat") -> str:
    """Publish a regular (non-queue) message and return the message_id."""
    resp = http.post(
        f"{url}/v1/publish",
        json={
            "channel": channel,
            "sender": "admin",
            "msg_type": "text",
            "payload": "hello world",
        },
    )
    assert resp.status_code == 200
    return resp.json()["message_id"]


class TestQueueStatus:
    """GET /v1/queue/status endpoint."""

    def test_queue_status_unclaimed(self, server_url: str) -> None:
        http = HttpClient()
        msg_id = _publish_queue_msg(http, server_url)

        resp = http.get(f"{server_url}/v1/queue/status?message_id={msg_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["status"]["status"] == "unclaimed"
        http.close()

    def test_queue_status_claimed(self, server_url: str) -> None:
        http = HttpClient()
        msg_id = _publish_queue_msg(http, server_url)

        # Claim the message
        resp = http.post(
            f"{server_url}/v1/queue/claim",
            json={"channel": "jobs", "claimed_by": "worker-1"},
        )
        assert resp.status_code == 200
        assert resp.json()["claimed"] is True

        # Check status
        resp = http.get(f"{server_url}/v1/queue/status?message_id={msg_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["status"]["status"] == "claimed"
        assert data["status"]["claimed_by"] == "worker-1"
        http.close()

    def test_queue_status_completed(self, server_url: str) -> None:
        http = HttpClient()
        _publish_queue_msg(http, server_url)

        claim_resp = http.post(
            f"{server_url}/v1/queue/claim",
            json={"channel": "jobs", "claimed_by": "worker"},
        )
        assert claim_resp.json()["claimed"] is True
        claimed_id = claim_resp.json()["result"]["message"]["id"]

        ack_resp = http.post(
            f"{server_url}/v1/queue/ack",
            json={"message_id": claimed_id, "claimed_by": "worker"},
        )
        assert ack_resp.json()["acked"] is True

        resp = http.get(f"{server_url}/v1/queue/status?message_id={claimed_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["status"]["status"] == "completed"
        assert data["status"]["claimed_by"] == "worker"
        http.close()

    def test_queue_status_not_queue(self, server_url: str) -> None:
        http = HttpClient()
        msg_id = _publish_regular_msg(http, server_url)

        resp = http.get(f"{server_url}/v1/queue/status?message_id={msg_id}")
        assert resp.status_code == 404
        data = resp.json()
        assert data["error"] == "Not Found"
        http.close()

    def test_queue_status_missing_param(self, server_url: str) -> None:
        http = HttpClient()

        resp = http.get(f"{server_url}/v1/queue/status")
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"] == "Bad Request"
        assert "message_id" in data["message"]
        http.close()


class TestQueueClaimNewPath:
    """POST /v1/queue/claim canonical path."""

    def test_queue_claim_new_path(self, server_url: str) -> None:
        http = HttpClient()
        _publish_queue_msg(http, server_url, channel="claim-test")

        resp = http.post(
            f"{server_url}/v1/queue/claim",
            json={"channel": "claim-test", "claimed_by": "worker-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["claimed"] is True
        assert data["result"]["claimed_by"] == "worker-1"
        http.close()


class TestQueueAckNewPath:
    """POST /v1/queue/ack canonical path."""

    def test_queue_ack_new_path(self, server_url: str) -> None:
        http = HttpClient()
        _publish_queue_msg(http, server_url, channel="ack-test")

        # Claim first
        resp = http.post(
            f"{server_url}/v1/queue/claim",
            json={"channel": "ack-test", "claimed_by": "worker-1"},
        )
        assert resp.status_code == 200
        msg_id = resp.json()["result"]["message"]["id"]

        # Ack via new path
        resp = http.post(
            f"{server_url}/v1/queue/ack",
            json={"message_id": msg_id, "claimed_by": "worker-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["acked"] is True
        assert data["result"]["status"] == "completed"
        http.close()


class TestDeprecatedAliases:
    """POST /v1/claim and POST /v1/ack still work (backward compat)."""

    def test_old_claim_path_still_works(self, server_url: str) -> None:
        http = HttpClient()
        _publish_queue_msg(http, server_url, channel="old-claim")

        resp = http.post(
            f"{server_url}/v1/claim",
            json={"channel": "old-claim", "claimed_by": "worker-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["claimed"] is True
        http.close()

    def test_old_ack_path_still_works(self, server_url: str) -> None:
        http = HttpClient()
        _publish_queue_msg(http, server_url, channel="old-ack")

        # Claim via old path
        resp = http.post(
            f"{server_url}/v1/claim",
            json={"channel": "old-ack", "claimed_by": "worker-1"},
        )
        assert resp.status_code == 200
        msg_id = resp.json()["result"]["message"]["id"]

        # Ack via old path
        resp = http.post(
            f"{server_url}/v1/ack",
            json={"message_id": msg_id, "claimed_by": "worker-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["acked"] is True
        http.close()
