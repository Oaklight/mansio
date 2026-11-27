"""Contract parsing tests for the bus HTTP API."""

from __future__ import annotations

import pytest
from mansio_core.contracts import (
    ACLAddRequest,
    ACLSetRequest,
    AdminCleanupRequest,
    AdminCompactRequest,
    ChannelCreateRequest,
    ChannelsRequest,
    ContractError,
    PresenceHeartbeatRequest,
    PresenceListRequest,
    PublishRequest,
    QueryRequest,
    QueueAckRequest,
    QueueClaimRequest,
    QueueStatusRequest,
    RegistryLookupRequest,
    parse_acl_add,
    parse_acl_set,
    parse_admin_cleanup,
    parse_admin_compact,
    parse_channel_create,
    parse_channels,
    parse_heartbeat,
    parse_presence_list,
    parse_publish,
    parse_query,
    parse_queue_ack,
    parse_queue_claim,
    parse_queue_status,
    parse_registry_lookup,
)


def _publish(**overrides):
    body = {
        "channel": "general",
        "sender": "alice",
        "msg_type": "text",
        "payload": "hello",
    }
    body.update(overrides)
    return body


class TestPublishContract:
    def test_valid_request_normalises_fields(self) -> None:
        request = parse_publish(_publish(channel=" general ", msg_type=" text "))
        assert isinstance(request, PublishRequest)
        assert request.channel == "general"
        assert request.msg_type == "text"
        assert request.queue is False

    def test_optional_fields(self) -> None:
        request = parse_publish(
            _publish(
                queue=True,
                parent_id="parent",
                intent="REQUIRES_RESPONSE",
                metadata={"tags": [" a ", "b"]},
            )
        )
        assert request.queue is True
        assert request.parent_id == "parent"
        assert request.intent == "REQUIRES_RESPONSE"
        assert request.metadata == {"tags": ["a", "b"]}

    def test_missing_fields(self) -> None:
        with pytest.raises(ContractError, match="Missing: msg_type, payload"):
            parse_publish({"channel": "general", "sender": "alice"})

    def test_non_object_body(self) -> None:
        with pytest.raises(ContractError, match="Request body must be a JSON object"):
            parse_publish([])

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ("", "Payload must be a non-empty string"),
            ("a\x00b", "Payload must not contain null bytes"),
            ("x" * (256 * 1024 + 1), "Payload exceeds maximum size of 256KB"),
        ],
    )
    def test_payload_errors(self, payload: str, message: str) -> None:
        with pytest.raises(ContractError, match=message):
            parse_publish(_publish(payload=payload))

    def test_msg_type_errors(self) -> None:
        with pytest.raises(ContractError, match="msg_type must be a non-empty string"):
            parse_publish(_publish(msg_type=" "))
        with pytest.raises(ContractError, match="msg_type must not contain null bytes"):
            parse_publish(_publish(msg_type="a\x00b"))
        with pytest.raises(ContractError, match="at most 64 characters"):
            parse_publish(_publish(msg_type="x" * 65))

    def test_metadata_errors(self) -> None:
        with pytest.raises(ContractError, match="metadata must be a JSON object or null"):
            parse_publish(_publish(metadata=[]))
        with pytest.raises(ContractError, match="Metadata must be JSON-serializable"):
            parse_publish(_publish(metadata={"x": object()}))
        with pytest.raises(ContractError, match="Metadata exceeds maximum size of 16KB"):
            parse_publish(_publish(metadata={"x": "y" * 16384}))

    def test_tag_errors(self) -> None:
        with pytest.raises(ContractError, match="Tags must be a list of strings"):
            parse_publish(_publish(metadata={"tags": "nope"}))
        with pytest.raises(ContractError, match="Tags must be non-empty strings"):
            parse_publish(_publish(metadata={"tags": [" "]}))
        with pytest.raises(ContractError, match="Maximum 20 tags allowed"):
            parse_publish(_publish(metadata={"tags": [str(i) for i in range(21)]}))
        with pytest.raises(ContractError, match="at most 64 characters"):
            parse_publish(_publish(metadata={"tags": ["x" * 65]}))

    def test_channel_and_sender_types(self) -> None:
        with pytest.raises(ContractError, match="Channel must be a string"):
            parse_publish(_publish(channel=1))
        with pytest.raises(ContractError, match="Channel must not contain null bytes"):
            parse_publish(_publish(channel="a\x00b"))
        with pytest.raises(ContractError, match="Channel must be a non-empty string"):
            parse_publish(_publish(channel=""))
        with pytest.raises(ContractError, match="Channel must be a non-empty string"):
            parse_publish(_publish(channel="   "))
        with pytest.raises(ContractError, match="Sender must be a string without null bytes"):
            parse_publish(_publish(sender=None))
        with pytest.raises(ContractError, match="Sender must be a non-empty string"):
            parse_publish(_publish(sender="   "))

    def test_optional_reference_fields_must_be_strings_or_null(self) -> None:
        request = parse_publish(_publish(parent_id=None, intent=None))
        assert request.parent_id is None
        assert request.intent is None
        with pytest.raises(ContractError, match="'parent_id' must be a string or null"):
            parse_publish(_publish(parent_id=1))
        with pytest.raises(ContractError, match="'intent' must be a string or null"):
            parse_publish(_publish(intent=123))


class TestQueryContract:
    def test_defaults(self) -> None:
        request = parse_query({"channel": ["general"]}, max_limit=1000)
        assert request == QueryRequest(channel="general")

    def test_all_fields(self) -> None:
        request = parse_query(
            {
                "channel": ["general"],
                "after": ["msg-1"],
                "limit": ["5"],
                "order": ["newest"],
                "offset": ["2"],
                "msg_type": ["text"],
                "thread_id": ["root"],
                "intent": ["FYI_ONLY"],
            },
            max_limit=1000,
        )
        assert request.limit == 5
        assert request.offset == 2
        assert request.order == "newest"
        assert request.thread_id == "root"

    def test_limit_is_capped(self) -> None:
        assert parse_query({"channel": "general", "limit": "9999"}, max_limit=100).limit == 100

    def test_errors(self) -> None:
        with pytest.raises(ContractError, match="Query param 'channel' required"):
            parse_query({}, max_limit=100)
        with pytest.raises(ContractError, match="'limit' must be an integer"):
            parse_query({"channel": "general", "limit": "x"}, max_limit=100)
        with pytest.raises(ContractError, match="'limit' must be at least 1"):
            parse_query({"channel": "general", "limit": "0"}, max_limit=100)
        with pytest.raises(ContractError, match="'offset' must be an integer"):
            parse_query({"channel": "general", "offset": "x"}, max_limit=100)
        with pytest.raises(ContractError, match="'offset' must be non-negative"):
            parse_query({"channel": "general", "offset": "-1"}, max_limit=100)
        with pytest.raises(ContractError, match="'order' must be 'oldest' or 'newest'"):
            parse_query({"channel": "general", "order": "sideways"}, max_limit=100)

    def test_channel_validator_runs_before_limits(self) -> None:
        def reject(channel: str) -> None:
            raise ContractError("bad channel")

        with pytest.raises(ContractError, match="bad channel"):
            parse_query({"channel": "dm:x", "limit": "bad"}, max_limit=100, validate_channel=reject)


class TestChannelContracts:
    def test_channels_detail(self) -> None:
        assert parse_channels({"detail": ["true"]}) == ChannelsRequest(detail=True)
        assert parse_channels({"detail": ["TRUE"]}).detail is True
        assert parse_channels({}).detail is False

    def test_channel_create(self) -> None:
        request = parse_channel_create({"name": " ops ", "owner": "alice"})
        assert request == ChannelCreateRequest(name="ops", owner="alice")
        with pytest.raises(ContractError, match="'name' is required"):
            parse_channel_create({"name": " "})
        with pytest.raises(ContractError, match="visibility must be"):
            parse_channel_create({"name": "ops", "visibility": "secret"})


class TestACLContracts:
    def test_set(self) -> None:
        request = parse_acl_set(
            {"acl": [{"user_id": "alice", "permission": "member"}, {"user_id": "bob"}]}
        )
        assert isinstance(request, ACLSetRequest)
        assert [entry.user_id for entry in request.entries] == ["alice", "bob"]
        assert request.entries[1].permission == "read"

    def test_set_errors(self) -> None:
        with pytest.raises(ContractError, match="'acl' must be a list"):
            parse_acl_set({"acl": "nope"})
        with pytest.raises(ContractError, match="Each ACL entry must be an object"):
            parse_acl_set({"acl": ["alice"]})
        with pytest.raises(ContractError, match="requires 'user_id'"):
            parse_acl_set({"acl": [{"permission": "read"}]})
        with pytest.raises(ContractError, match="permission must be one of:"):
            parse_acl_set({"acl": [{"user_id": "alice", "permission": "root"}]})

    def test_add(self) -> None:
        assert parse_acl_add({"user_id": " alice "}) == ACLAddRequest(user_id="alice")
        with pytest.raises(ContractError, match="'user_id' is required"):
            parse_acl_add({"user_id": " "})


class TestQueueContracts:
    def test_claim(self) -> None:
        request = parse_queue_claim({"channel": " jobs ", "lease_seconds": "60"})
        assert request == QueueClaimRequest(channel="jobs", lease_seconds=60)
        with pytest.raises(ContractError, match="Missing 'channel'"):
            parse_queue_claim({})
        with pytest.raises(ContractError, match="'lease_seconds' must be an integer"):
            parse_queue_claim({"channel": "jobs", "lease_seconds": "soon"})

    def test_ack(self) -> None:
        request = parse_queue_ack({"message_id": "m1", "claimed_by": " alice "})
        assert request == QueueAckRequest(message_id="m1", claimed_by="alice")
        with pytest.raises(ContractError, match="Missing 'message_id'"):
            parse_queue_ack({})

    def test_status(self) -> None:
        assert parse_queue_status({"message_id": ["m1"]}) == QueueStatusRequest(message_id="m1")
        with pytest.raises(ContractError, match="Query param 'message_id' required"):
            parse_queue_status({})


class TestPresenceContracts:
    def test_heartbeat(self) -> None:
        request = parse_heartbeat({"user_id": " alice ", "metadata": {"display_name": "A"}})
        assert request == PresenceHeartbeatRequest(user_id="alice", metadata={"display_name": "A"})
        with pytest.raises(ContractError, match="user_id required"):
            parse_heartbeat({"user_id": " "})

    def test_list(self) -> None:
        assert parse_presence_list({}) == PresenceListRequest()
        assert parse_presence_list({"timeout": ["5"]}).timeout == 5
        with pytest.raises(ContractError, match="'timeout' must be an integer"):
            parse_presence_list({"timeout": "x"})
        with pytest.raises(ContractError, match="'timeout' must be at least 1"):
            parse_presence_list({"timeout": "0"})


class TestAdminContracts:
    def test_registry_lookup(self) -> None:
        assert parse_registry_lookup({"user_id": [" alice "]}) == RegistryLookupRequest(
            user_id="alice"
        )
        with pytest.raises(ContractError, match="Query param 'user_id' required"):
            parse_registry_lookup({})

    def test_cleanup(self) -> None:
        assert parse_admin_cleanup(
            {"pattern": " test:* ", "older_than": "2026-01-01"}
        ) == AdminCleanupRequest(pattern="test:*", older_than="2026-01-01")
        with pytest.raises(ContractError, match="'pattern' field is required"):
            parse_admin_cleanup({"pattern": " "})

    def test_compact(self) -> None:
        assert parse_admin_compact(
            {"channel": " ops ", "max_messages": 10, "keep_latest_per_sender": True}
        ) == AdminCompactRequest(channel="ops", max_messages=10, keep_latest_per_sender=True)
        with pytest.raises(ContractError, match="'channel' field is required"):
            parse_admin_compact({})
        with pytest.raises(ContractError, match="max_messages must be a positive integer"):
            parse_admin_compact({"channel": "ops", "max_messages": 0})
