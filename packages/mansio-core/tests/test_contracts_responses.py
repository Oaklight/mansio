"""Golden tests: response DTOs reproduce the pre-M3 route bodies.

Each test compares the DTO's document with the dict literal the route
returned before M3a, so a field rename, reorder or accidental omission
fails here.
"""

from __future__ import annotations

from mansio_core.contracts import (
    ACLAddEntry,
    ACLAddResult,
    ACLRemoveResult,
    ACLResult,
    ACLSetResult,
    AdminCleanupResult,
    AdminCompactResult,
    AuthCheckResult,
    ChannelResult,
    ChannelsResult,
    DeleteChannelResult,
    DeleteMessageResult,
    ErrorResult,
    HealthResult,
    PresenceAgentResult,
    PresenceHeartbeatResult,
    PresenceResult,
    PublishResult,
    QueryResult,
    QueueAckResult,
    QueueClaimResult,
    QueueStatusResult,
    RegistryLookupResult,
)
from mansio_core.types import ACLEntry, ChannelMeta, ClaimResult, Message, UserPresence


def _message() -> Message:
    return Message(
        id="msg-1",
        channel="general",
        sender="alice",
        msg_type="text",
        payload="hello",
        timestamp="2026-01-01T00:00:00+00:00",
        metadata={"k": "v"},
        parent_id="parent-1",
        thread_id="root-1",
        intent="FYI_ONLY",
    )


def _message_dict() -> dict:
    return {
        "id": "msg-1",
        "channel": "general",
        "sender": "alice",
        "msg_type": "text",
        "payload": "hello",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "metadata": {"k": "v"},
        "parent_id": "parent-1",
        "thread_id": "root-1",
        "intent": "FYI_ONLY",
    }


def _claim_result(lease_until: str | None = "2026-01-01T00:05:00+00:00") -> ClaimResult:
    return ClaimResult(
        message=_message(),
        status="claimed",
        claimed_by="alice",
        claimed_at="2026-01-01T00:00:00+00:00",
        lease_until=lease_until,
    )


def _claim_result_dict() -> dict:
    return {
        "message": _message_dict(),
        "status": "claimed",
        "claimed_by": "alice",
        "claimed_at": "2026-01-01T00:00:00+00:00",
        "lease_until": "2026-01-01T00:05:00+00:00",
    }


class TestSimpleResults:
    def test_health(self) -> None:
        assert HealthResult().to_dict() == {"status": "ok"}

    def test_publish_without_warning(self) -> None:
        assert PublishResult(message_id="m").to_dict() == {"message_id": "m"}

    def test_publish_with_warning(self) -> None:
        assert PublishResult(message_id="m", warning="w").to_dict() == {
            "message_id": "m",
            "warning": "w",
        }

    def test_registry_lookup(self) -> None:
        assert RegistryLookupResult(found=True, user_id="a").to_dict() == {
            "found": True,
            "user_id": "a",
        }

    def test_auth_check(self) -> None:
        assert AuthCheckResult(auth_mode="required", has_tokens=True).to_dict() == {
            "auth_mode": "required",
            "has_tokens": True,
        }

    def test_error(self) -> None:
        assert ErrorResult(error="Bad Request", message="m").to_dict() == {
            "error": "Bad Request",
            "message": "m",
        }


class TestQueryAndChannels:
    def test_query_body_and_key_order(self) -> None:
        document = QueryResult(
            messages=[_message()], count=1, total=7, offset=2, has_more=False
        ).to_dict()
        assert document == {
            "messages": [_message_dict()],
            "count": 1,
            "total": 7,
            "offset": 2,
            "has_more": False,
        }
        assert list(document) == ["messages", "count", "total", "offset", "has_more"]

    def test_channels_names(self) -> None:
        assert ChannelsResult(channels=["general"]).to_dict() == {"channels": ["general"]}

    def test_channels_detail_dicts_pass_through(self) -> None:
        detail = [{"name": "general", "message_count": 3}]
        assert ChannelsResult(channels=detail).to_dict() == {"channels": detail}


class TestChannelsAndAcl:
    def test_channel_meta(self) -> None:
        meta = ChannelMeta(
            name="general", owner="alice", visibility="public", created_at="2026-01-01"
        )
        assert ChannelResult(channel=meta).to_dict() == {
            "channel": {
                "name": "general",
                "owner": "alice",
                "visibility": "public",
                "created_at": "2026-01-01",
            }
        }

    def test_acl_list(self) -> None:
        entry = ACLEntry(
            channel="general",
            user_id="alice",
            permission="read",
            granted_at="2026-01-01",
            granted_by=None,
        )
        assert ACLResult(acl=[entry]).to_dict() == {
            "acl": [
                {
                    "channel": "general",
                    "user_id": "alice",
                    "permission": "read",
                    "granted_at": "2026-01-01",
                    "granted_by": None,
                }
            ]
        }

    def test_acl_set(self) -> None:
        assert ACLSetResult(status="ok", count=2).to_dict() == {"status": "ok", "count": 2}

    def test_acl_add_echoes_three_fields(self) -> None:
        assert ACLAddResult(
            status="ok", entry=ACLAddEntry(channel="c", user_id="u", permission="read")
        ).to_dict() == {
            "status": "ok",
            "entry": {"channel": "c", "user_id": "u", "permission": "read"},
        }

    def test_acl_remove(self) -> None:
        assert ACLRemoveResult().to_dict() == {"status": "ok"}


class TestDeletionsAndAdmin:
    def test_delete_channel(self) -> None:
        assert DeleteChannelResult(deleted=4, channel="c").to_dict() == {
            "deleted": 4,
            "channel": "c",
        }

    def test_delete_message(self) -> None:
        assert DeleteMessageResult(deleted=True, message_id="m").to_dict() == {
            "deleted": True,
            "message_id": "m",
        }

    def test_admin_cleanup(self) -> None:
        document = AdminCleanupResult(
            channels_deleted=2, messages_deleted=9, channels=["a", "b"]
        ).to_dict()
        assert document == {
            "channels_deleted": 2,
            "messages_deleted": 9,
            "channels": ["a", "b"],
        }
        assert list(document) == ["channels_deleted", "messages_deleted", "channels"]

    def test_admin_compact(self) -> None:
        assert AdminCompactResult(channel="c", messages_removed=3).to_dict() == {
            "channel": "c",
            "messages_removed": 3,
        }


class TestQueue:
    def test_claim_empty(self) -> None:
        assert QueueClaimResult(claimed=False).to_dict() == {"claimed": False}

    def test_claim_result_with_lease(self) -> None:
        document = QueueClaimResult(claimed=True, result=_claim_result()).to_dict()
        assert document == {"claimed": True, "result": _claim_result_dict()}
        assert list(document) == ["claimed", "result"]

    def test_claim_result_without_lease(self) -> None:
        document = QueueClaimResult(claimed=True, result=_claim_result(None)).to_dict()
        expected = _claim_result_dict()
        del expected["lease_until"]
        assert document == {"claimed": True, "result": expected}

    def test_ack_empty(self) -> None:
        assert QueueAckResult(acked=False).to_dict() == {"acked": False}

    def test_ack_result(self) -> None:
        assert QueueAckResult(acked=True, result=_claim_result()).to_dict() == {
            "acked": True,
            "result": _claim_result_dict(),
        }

    def test_status(self) -> None:
        assert QueueStatusResult(found=True, status={"status": "claimed"}).to_dict() == {
            "found": True,
            "status": {"status": "claimed"},
        }


class TestPresence:
    def test_heartbeat(self) -> None:
        assert PresenceHeartbeatResult().to_dict() == {"ok": True}

    def test_list(self) -> None:
        presence = UserPresence(
            user_id="alice",
            status="online",
            last_seen="2026-01-01",
            metadata={"display_name": "A"},
        )
        assert PresenceResult(agents=[presence]).to_dict() == {
            "agents": [
                {
                    "user_id": "alice",
                    "status": "online",
                    "last_seen": "2026-01-01",
                    "metadata": {"display_name": "A"},
                }
            ]
        }

    def test_agent(self) -> None:
        document = PresenceAgentResult(
            user_id="alice", status="online", last_seen="2026-01-01", metadata=None
        ).to_dict()
        assert document == {
            "user_id": "alice",
            "status": "online",
            "last_seen": "2026-01-01",
            "metadata": None,
        }
        assert list(document) == ["user_id", "status", "last_seen", "metadata"]
