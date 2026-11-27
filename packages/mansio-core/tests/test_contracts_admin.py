"""Tests for the admin API contracts (parsers and response documents)."""

from __future__ import annotations

import pytest
from mansio_core.contracts import ContractError
from mansio_core.contracts.admin import (
    AdminChannelDetail,
    AdminChannelsResult,
    AdminChannelSummary,
    AdminMessagesQuery,
    AdminMessagesResult,
    AdminPrefixCreatedResult,
    AdminPrefixDeletedResult,
    AdminPrefixesResult,
    AdminStatsResult,
    AdminSubscriptionsResult,
    AdminSystemResult,
    AdminThroughputResult,
    AdminTokenCreatedResult,
    AdminTokenDeletedResult,
    AdminTokenRotatedResult,
    AdminTokensResult,
    AdminUserDeletedResult,
    AdminUserDetailResult,
    AdminUserRegisteredResult,
    AdminUsersResult,
    AdminUserTokensResult,
    parse_admin_messages,
    parse_prefix_create,
    parse_token_create,
    parse_user_create,
    parse_user_token_create,
)


class TestAdminMessageQuery:
    def test_defaults(self) -> None:
        query = parse_admin_messages({"channel": ["general"]})
        assert query == AdminMessagesQuery(channel="general")

    def test_all_fields(self) -> None:
        query = parse_admin_messages(
            {
                "channel": ["general"],
                "sender": ["alice"],
                "msg_type": ["text"],
                "limit": ["5"],
            }
        )
        assert query == AdminMessagesQuery(
            channel="general", sender="alice", msg_type="text", limit=5
        )

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({}, "Query param 'channel' required"),
            ({"channel": ["c"], "limit": ["x"]}, "'limit' must be an integer"),
            ({"channel": ["c"], "limit": ["0"]}, "'limit' must be at least 1"),
        ],
    )
    def test_errors(self, params: dict, message: str) -> None:
        with pytest.raises(ContractError, match=message):
            parse_admin_messages(params)


class TestAdminRequestParsers:
    def test_prefix_normalises(self) -> None:
        request = parse_prefix_create({"prefix": " Ops ", "label": "ops"})
        assert request.prefix == "ops"
        assert request.label == "ops"

    def test_prefix_errors(self) -> None:
        with pytest.raises(ContractError, match="Prefix must be 2-32"):
            parse_prefix_create({"prefix": "x"})
        with pytest.raises(ContractError, match="Prefix must be 2-32"):
            parse_prefix_create({"prefix": "Ops!"})
        with pytest.raises(ContractError, match="'label' must be a string"):
            parse_prefix_create({"prefix": "ops", "label": 1})

    def test_user_create(self) -> None:
        request = parse_user_create({"user_id": " agent-a "})
        assert request.user_id == "agent-a"
        assert request.label == "initial token"

    def test_user_create_errors(self) -> None:
        with pytest.raises(ContractError, match="user_id is required"):
            parse_user_create({"user_id": "  "})
        with pytest.raises(ContractError, match="user_id must be 3-64"):
            parse_user_create({"user_id": "A"})

    def test_token_create_user(self) -> None:
        request = parse_token_create({"user_id": " agent-a ", "label": "l"})
        assert request.user_id == "agent-a"
        assert request.supertoken is False
        assert request.label == "l"

    def test_token_create_supertoken(self) -> None:
        request = parse_token_create({"supertoken": True})
        assert request.user_id is None
        assert request.supertoken is True

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({"supertoken": "yes"}, "supertoken must be a boolean"),
            (
                {"supertoken": True, "user_id": "agent-a"},
                "user_id must be omitted when supertoken is true",
            ),
            ({"user_id": ""}, "user_id must be a non-empty string"),
            ({}, "user_id must be a non-empty string"),
        ],
    )
    def test_token_create_errors(self, body: dict, message: str) -> None:
        with pytest.raises(ContractError, match=message):
            parse_token_create(body)

    def test_user_token_label(self) -> None:
        assert parse_user_token_create({"label": "l"}) == "l"
        assert parse_user_token_create({}) == ""
        with pytest.raises(ContractError, match="'label' must be a string"):
            parse_user_token_create({"label": 1})


class TestAdminResponseDocuments:
    def test_stats_is_the_dict_itself(self) -> None:
        assert AdminStatsResult(stats={"messages": 3}).to_dict() == {"messages": 3}

    def test_throughput(self) -> None:
        document = AdminThroughputResult(window_seconds=60, buckets=[{"t": 1}]).to_dict()
        assert document == {"window_seconds": 60, "buckets": [{"t": 1}]}
        assert list(document) == ["window_seconds", "buckets"]

    def test_channels_nested_summary(self) -> None:
        summary = AdminChannelSummary(
            name="general",
            message_count=2,
            sender_count=1,
            subscription_count=0,
            last_message_time="2026-01-01",
        )
        assert AdminChannelsResult(channels=[summary]).to_dict() == {
            "channels": [
                {
                    "name": "general",
                    "message_count": 2,
                    "sender_count": 1,
                    "subscription_count": 0,
                    "last_message_time": "2026-01-01",
                }
            ]
        }

    def test_channel_detail(self) -> None:
        document = AdminChannelDetail(
            name="general",
            message_count=2,
            senders=["alice"],
            msg_types=[{"msg_type": "text", "count": 2}],
            last_message_time="2026-01-01",
        ).to_dict()
        assert list(document) == [
            "name",
            "message_count",
            "senders",
            "msg_types",
            "last_message_time",
        ]

    def test_messages(self) -> None:
        assert AdminMessagesResult(count=1, messages=[{"id": "m"}]).to_dict() == {
            "count": 1,
            "messages": [{"id": "m"}],
        }

    def test_subscriptions(self) -> None:
        assert AdminSubscriptionsResult(
            total=2, channels=[{"channel": "c", "subscription_ids": ["a", "b"], "count": 2}]
        ).to_dict() == {
            "total": 2,
            "channels": [{"channel": "c", "subscription_ids": ["a", "b"], "count": 2}],
        }

    def test_system(self) -> None:
        assert AdminSystemResult(
            version="0.3.0", backend={"type": "sqlite"}, tokens={"configured": 1}
        ).to_dict() == {
            "version": "0.3.0",
            "backend": {"type": "sqlite"},
            "tokens": {"configured": 1},
        }

    def test_prefixes_and_mutations(self) -> None:
        assert AdminPrefixesResult(prefixes=[{"prefix": "ops"}], enabled=True).to_dict() == {
            "prefixes": [{"prefix": "ops"}],
            "enabled": True,
        }
        assert AdminPrefixCreatedResult(prefix={"prefix": "ops"}).to_dict() == {
            "ok": True,
            "prefix": {"prefix": "ops"},
        }
        assert AdminPrefixDeletedResult(deleted="ops").to_dict() == {
            "ok": True,
            "deleted": "ops",
        }

    def test_users(self) -> None:
        assert AdminUsersResult(users=[{"user_id": "a"}]).to_dict() == {"users": [{"user_id": "a"}]}
        assert AdminUserRegisteredResult(user_id="a", token={"token": "mst-x"}).to_dict() == {
            "ok": True,
            "user_id": "a",
            "token": {"token": "mst-x"},
        }
        assert AdminUserDetailResult(
            user_id="a",
            tokens=[{"id": "t"}],
            status="online",
            last_seen="2026-01-01",
        ).to_dict() == {
            "user_id": "a",
            "tokens": [{"id": "t"}],
            "status": "online",
            "last_seen": "2026-01-01",
        }
        assert AdminUserDeletedResult(deleted_tokens=2).to_dict() == {
            "ok": True,
            "deleted_tokens": 2,
        }
        assert AdminUserTokensResult(user_id="a", tokens=[]).to_dict() == {
            "user_id": "a",
            "tokens": [],
        }

    def test_token_created_ordinary_omits_supertoken(self) -> None:
        document = AdminTokenCreatedResult(token={"token": "mst-x"}).to_dict()
        assert document == {"ok": True, "token": {"token": "mst-x"}}
        assert list(document) == ["ok", "token"]

    def test_token_created_supertoken_keeps_wire_order(self) -> None:
        document = AdminTokenCreatedResult(supertoken=True, token={"token": "mst-x"}).to_dict()
        assert document == {
            "ok": True,
            "supertoken": True,
            "token": {"token": "mst-x"},
        }
        assert list(document) == ["ok", "supertoken", "token"]

    def test_tokens_and_mutations(self) -> None:
        assert AdminTokensResult(tokens=[], enabled=False).to_dict() == {
            "tokens": [],
            "enabled": False,
        }
        assert AdminTokenDeletedResult(deleted="t").to_dict() == {
            "ok": True,
            "deleted": "t",
        }
        assert AdminTokenRotatedResult(token={"token": "mst-y"}).to_dict() == {
            "ok": True,
            "token": {"token": "mst-y"},
        }
