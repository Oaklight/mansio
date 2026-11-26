"""Tests for the shared channel authorization policy (#247 slice 2)."""

from __future__ import annotations

import pytest
from mansio_core import ACLEntry, Bus, MemoryBackend
from mansio_core.authz import (
    ChannelAuthorizer,
    is_channel_member,
    is_private_channel,
    user_involved,
)


@pytest.fixture()
def bus():
    b = Bus(MemoryBackend())
    yield b
    b.close()


@pytest.fixture()
def authz(bus):
    return ChannelAuthorizer(bus)


class TestStructuralHelpers:
    def test_private_channels(self) -> None:
        assert is_private_channel("dm:a:b")
        assert is_private_channel("notebook:alice")
        assert is_private_channel("memory:alice")
        assert not is_private_channel("general")
        assert not is_private_channel("broadcast:news")
        assert not is_private_channel("_system:agents")

    def test_membership(self) -> None:
        assert is_channel_member("alice", "notebook:alice")
        assert is_channel_member("bob", "dm:alice:bob")
        assert is_channel_member("alice", "dm:alice:bob")
        assert not is_channel_member("eve", "dm:alice:bob")
        assert not is_channel_member("notebook", "notebook:alice")
        assert not is_channel_member("alice", "general")

    def test_user_involved(self) -> None:
        assert user_involved("anyone", "general", "other")
        assert user_involved("alice", "dm:alice:bob", "bob")
        assert not user_involved("eve", "dm:alice:bob", "alice")


class TestRead:
    def test_public_channel_is_readable(self, authz) -> None:
        assert authz.can_read("stranger", "general")
        assert authz.can_list_channel("stranger", "general")

    def test_private_channel_requires_membership(self, authz) -> None:
        assert authz.can_read("alice", "notebook:alice")
        assert not authz.can_read("bob", "notebook:alice")
        assert authz.can_read("bob", "dm:alice:bob")
        assert not authz.can_read("eve", "dm:alice:bob")
        assert not authz.can_list_channel("eve", "dm:alice:bob")


class TestPublish:
    def test_plain_channel_open(self, authz) -> None:
        assert authz.can_publish("anyone", "general")

    def test_private_channels_require_membership(self, authz) -> None:
        assert authz.can_publish("alice", "notebook:alice")
        assert not authz.can_publish("bob", "notebook:alice")
        assert authz.publish_denial("bob", "notebook:alice") == (
            "Channel 'notebook:alice' belongs to agent 'alice'"
        )
        assert authz.can_publish("bob", "dm:alice:bob")
        assert not authz.can_publish("eve", "dm:alice:bob")

    def test_system_channels(self, authz) -> None:
        assert authz.can_publish("milo", "_system:agents", "presence")
        assert not authz.can_publish("milo", "_system:agents", "chat")
        assert authz.can_publish("milo", "_system:cursors:milo")
        assert not authz.can_publish("milo", "_system:cursors:other")
        assert authz.publish_denial("milo", "_system:agents", "chat") == (
            "Channel '_system:agents' is reserved for system use"
        )

    def test_broadcast_owner_and_acl(self, bus, authz) -> None:
        bus.ensure_channel("broadcast:news", owner="alice")
        assert authz.can_publish("alice", "broadcast:news")
        assert not authz.can_publish("bob", "broadcast:news")

        bus.add_acl_entry(ACLEntry(channel="broadcast:news", user_id="bob", permission="member"))
        assert authz.can_publish("bob", "broadcast:news")

        bus.add_acl_entry(ACLEntry(channel="broadcast:news", user_id="carol", permission="read"))
        assert not authz.can_publish("carol", "broadcast:news")


class TestDelete:
    def test_private_channel_members_may_delete(self, authz) -> None:
        assert authz.can_delete_channel("alice", "notebook:alice")
        assert authz.can_delete_channel("bob", "dm:alice:bob")
        assert not authz.can_delete_channel("eve", "dm:alice:bob")
        assert authz.delete_channel_denial("eve", "dm:alice:bob") == (
            "Token for 'eve' cannot delete channel 'dm:alice:bob'"
        )

    def test_public_and_system_channels_are_closed(self, authz) -> None:
        assert not authz.can_delete_channel("alice", "general")
        assert authz.delete_channel_denial("alice", "general") == (
            "Scoped tokens cannot delete shared channels"
        )
        assert not authz.can_delete_channel("alice", "_system:agents")
        assert "system channel" in authz.delete_channel_denial("alice", "_system:agents")

    def test_message_delete_is_sender_only(self, authz) -> None:
        from mansio_core.types import Message

        msg = Message(
            id="m1",
            channel="general",
            sender="alice",
            msg_type="chat",
            payload="hi",
            timestamp="2026-01-01T00:00:00Z",
        )
        assert authz.can_delete_message("alice", msg)
        assert not authz.can_delete_message("bob", msg)
        assert not authz.can_delete_message("alice", None)


class TestManageAcl:
    def test_owner_may_manage(self, bus, authz) -> None:
        bus.create_channel("ch", "alice", visibility="private")
        assert authz.can_manage_acl("alice", "ch")
        assert not authz.can_manage_acl("bob", "ch")

    def test_manage_grant_is_honored(self, bus, authz) -> None:
        bus.create_channel("ch", "alice", visibility="private")
        bus.add_acl_entry(ACLEntry(channel="ch", user_id="bob", permission="manage"))
        assert authz.can_manage_acl("bob", "ch")
