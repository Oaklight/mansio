"""Typed wire contracts for the bus HTTP API.

Request dataclasses are the single definition of each request document:
parsers in :mod:`mansio_core.contracts.parsers` turn decoded JSON and query
values into them, and the route handlers consume the parsed object.
Response dataclasses are what the handlers build: each one serializes to the
document the route returns, so the wire shape has one definition.  No type
here exposes a backend-internal object.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Literal

from mansio_core.contracts.sse import message_to_dict
from mansio_core.types import ACLEntry, ChannelMeta, ClaimResult, Message, UserPresence


def _wire_value(value: Any) -> Any:
    """Convert a nested value to its JSON form."""
    if isinstance(value, WireResult):
        return value.to_dict()
    if isinstance(value, Message):
        return message_to_dict(value)
    if isinstance(value, ClaimResult):
        data: dict[str, Any] = {
            "message": message_to_dict(value.message),
            "status": value.status,
            "claimed_by": value.claimed_by,
            "claimed_at": value.claimed_at,
        }
        if value.lease_until is not None:
            data["lease_until"] = value.lease_until
        return data
    if isinstance(value, ChannelMeta):
        return {
            "name": value.name,
            "owner": value.owner,
            "visibility": value.visibility,
            "created_at": value.created_at,
        }
    if isinstance(value, UserPresence):
        return {
            "user_id": value.user_id,
            "status": value.status,
            "last_seen": value.last_seen,
            "metadata": value.metadata,
        }
    if isinstance(value, ACLEntry):
        return {
            "channel": value.channel,
            "user_id": value.user_id,
            "permission": value.permission,
            "granted_at": value.granted_at,
            "granted_by": value.granted_by,
        }
    if isinstance(value, list):
        return [_wire_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class WireResult:
    """Base for response documents.

    ``to_dict`` walks the fields in declaration order, which is the wire
    order, and converts nested contract values.  ``_omit_none`` names the
    optional fields that are left out of the document when unset.
    """

    _omit_none: ClassVar[frozenset[str]] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if value is None and item.name in self._omit_none:
                continue
            document[item.name] = _wire_value(value)
        return document


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """Body of ``POST /v1/publish``."""

    channel: str
    sender: str
    msg_type: str
    payload: str
    metadata: dict | None = None
    queue: bool = False
    parent_id: str | None = None
    intent: str | None = None


@dataclass(frozen=True, slots=True)
class PublishResult(WireResult):
    """Response of ``POST /v1/publish``."""

    _omit_none: ClassVar[frozenset[str]] = frozenset({"warning"})

    message_id: str
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """Query parameters of ``GET /v1/query``."""

    channel: str
    after: str | None = None
    limit: int = 100
    order: Literal["oldest", "newest"] = "oldest"
    offset: int = 0
    msg_type: str | None = None
    thread_id: str | None = None
    intent: str | None = None


@dataclass(frozen=True, slots=True)
class QueryResult(WireResult):
    """Response of ``GET /v1/query``."""

    messages: list[Message]
    count: int
    total: int
    offset: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class ChannelsRequest:
    """Query parameters of ``GET /v1/channels``."""

    detail: bool = False


@dataclass(frozen=True, slots=True)
class ChannelsResult(WireResult):
    """Response of ``GET /v1/channels``.

    ``channels`` is a list of names when ``detail`` is false and a list of
    channel detail objects when it is true.
    """

    channels: list[Any]


@dataclass(frozen=True, slots=True)
class ChannelCreateRequest:
    """Body of ``POST /v1/channels``."""

    name: str
    owner: str | None = None
    visibility: str = "public"


@dataclass(frozen=True, slots=True)
class ChannelResult(WireResult):
    """Response carrying one channel's metadata."""

    channel: ChannelMeta


@dataclass(frozen=True, slots=True)
class ACLResult(WireResult):
    """Response of ``GET /v1/channels/<channel>/acl``."""

    acl: list[ACLEntry]


@dataclass(frozen=True, slots=True)
class ACLEntrySpec:
    """One requested ACL entry, before timestamps and grantor are added."""

    user_id: str
    permission: str = "read"


@dataclass(frozen=True, slots=True)
class ACLSetRequest:
    """Body of ``PUT /v1/channels/<channel>/acl``."""

    entries: list[ACLEntrySpec]


@dataclass(frozen=True, slots=True)
class ACLAddRequest:
    """Body of ``POST /v1/channels/<channel>/acl``."""

    user_id: str
    permission: str = "read"


@dataclass(frozen=True, slots=True)
class DeleteChannelResult(WireResult):
    """Response of ``DELETE /v1/channels/<channel>``."""

    deleted: int
    channel: str


@dataclass(frozen=True, slots=True)
class DeleteMessageResult(WireResult):
    """Response of ``DELETE /v1/messages/<message_id>``."""

    deleted: bool
    message_id: str


@dataclass(frozen=True, slots=True)
class QueueClaimRequest:
    """Body of ``POST /v1/queue/claim``."""

    channel: str
    claimed_by: str | None = None
    lease_seconds: int = 300


@dataclass(frozen=True, slots=True)
class QueueAckRequest:
    """Body of ``POST /v1/queue/ack``."""

    message_id: str
    claimed_by: str | None = None


@dataclass(frozen=True, slots=True)
class QueueClaimResult(WireResult):
    """Response of ``POST /v1/queue/claim``."""

    _omit_none: ClassVar[frozenset[str]] = frozenset({"result"})

    claimed: bool
    result: ClaimResult | None = None


@dataclass(frozen=True, slots=True)
class QueueAckResult(WireResult):
    """Response of ``POST /v1/queue/ack``."""

    _omit_none: ClassVar[frozenset[str]] = frozenset({"result"})

    acked: bool
    result: ClaimResult | None = None


@dataclass(frozen=True, slots=True)
class QueueStatusRequest:
    """Query parameters of ``GET /v1/queue/status``."""

    message_id: str


@dataclass(frozen=True, slots=True)
class QueueStatusResult(WireResult):
    """Response of ``GET /v1/queue/status``."""

    found: bool
    status: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PresenceHeartbeatRequest:
    """Body of ``POST /v1/presence/heartbeat``."""

    user_id: str
    metadata: dict | None = None


@dataclass(frozen=True, slots=True)
class PresenceListRequest:
    """Query parameters of ``GET /v1/presence`` and ``/v1/presence/<user>``."""

    timeout: int = 120


@dataclass(frozen=True, slots=True)
class PresenceResult(WireResult):
    """Response of ``GET /v1/presence``."""

    agents: list[UserPresence]


@dataclass(frozen=True, slots=True)
class PresenceAgentResult(WireResult):
    """Response of ``GET /v1/presence/<user_id>``."""

    user_id: str
    status: str
    last_seen: str
    metadata: dict | None = None


@dataclass(frozen=True, slots=True)
class RegistryLookupRequest:
    """Query parameters of ``GET /v1/registry/lookup``."""

    user_id: str


@dataclass(frozen=True, slots=True)
class RegistryLookupResult(WireResult):
    """Response of ``GET /v1/registry/lookup``."""

    found: bool
    user_id: str


@dataclass(frozen=True, slots=True)
class AuthCheckResult(WireResult):
    """Response of ``GET /v1/auth/check``."""

    auth_mode: str
    has_tokens: bool


@dataclass(frozen=True, slots=True)
class HealthResult(WireResult):
    """Response of ``GET /health``."""

    status: str = "ok"


@dataclass(frozen=True, slots=True)
class AdminCleanupRequest:
    """Body of ``POST /v1/admin/channels/cleanup``."""

    pattern: str
    older_than: str | None = None


@dataclass(frozen=True, slots=True)
class AdminCleanupResult(WireResult):
    """Response of ``POST /v1/admin/channels/cleanup``."""

    channels_deleted: int
    messages_deleted: int
    channels: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AdminCompactRequest:
    """Body of ``POST /v1/admin/compact``."""

    channel: str
    max_messages: int | None = None
    keep_latest_per_sender: bool = False


@dataclass(frozen=True, slots=True)
class AdminCompactResult(WireResult):
    """Response of ``POST /v1/admin/compact``."""

    channel: str
    messages_removed: int


@dataclass(frozen=True, slots=True)
class ErrorResult(WireResult):
    """Error body shared by every bus route."""

    error: str
    message: str


@dataclass(frozen=True, slots=True)
class PresenceHeartbeatResult(WireResult):
    """Response of ``POST /v1/presence/heartbeat``."""

    ok: bool = True


@dataclass(frozen=True, slots=True)
class ACLSetResult(WireResult):
    """Response of ``PUT /v1/channels/<channel>/acl``."""

    status: str = "ok"
    count: int = 0


@dataclass(frozen=True, slots=True)
class ACLAddEntry(WireResult):
    """The entry echoed by ``POST /v1/channels/<channel>/acl``."""

    channel: str
    user_id: str
    permission: str


@dataclass(frozen=True, slots=True)
class ACLAddResult(WireResult):
    """Response of ``POST /v1/channels/<channel>/acl``."""

    status: str = "ok"
    entry: ACLAddEntry | None = None


@dataclass(frozen=True, slots=True)
class ACLRemoveResult(WireResult):
    """Response of ``DELETE /v1/channels/<channel>/acl/<user_id>``."""

    status: str = "ok"
