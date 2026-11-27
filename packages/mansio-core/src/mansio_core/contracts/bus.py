"""Typed wire contracts for the bus HTTP API.

Request dataclasses are the single definition of each request document:
parsers in :mod:`mansio_core.contracts.parsers` turn decoded JSON and query
values into them, and the route handlers consume the parsed object.
Response dataclasses document the wire schema, but handlers still return
plain dicts and do not construct them yet (planned for M3).  No type here
exposes a backend-internal object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from mansio_core.types import ACLEntry, ChannelMeta, ClaimResult, Message, UserPresence


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
class PublishResult:
    """Response of ``POST /v1/publish``."""

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
class QueryResult:
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
class ChannelsResult:
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
class ChannelResult:
    """Response carrying one channel's metadata."""

    channel: ChannelMeta


@dataclass(frozen=True, slots=True)
class ACLResult:
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
class DeleteChannelResult:
    """Response of ``DELETE /v1/channels/<channel>``."""

    deleted: int
    channel: str


@dataclass(frozen=True, slots=True)
class DeleteMessageResult:
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
class QueueClaimResult:
    """Response of ``POST /v1/queue/claim``."""

    claimed: bool
    result: ClaimResult | None = None


@dataclass(frozen=True, slots=True)
class QueueAckResult:
    """Response of ``POST /v1/queue/ack``."""

    acked: bool
    result: ClaimResult | None = None


@dataclass(frozen=True, slots=True)
class QueueStatusRequest:
    """Query parameters of ``GET /v1/queue/status``."""

    message_id: str


@dataclass(frozen=True, slots=True)
class QueueStatusResult:
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
class PresenceResult:
    """Response of ``GET /v1/presence``."""

    agents: list[UserPresence]


@dataclass(frozen=True, slots=True)
class PresenceAgentResult:
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
class RegistryLookupResult:
    """Response of ``GET /v1/registry/lookup``."""

    found: bool
    user_id: str


@dataclass(frozen=True, slots=True)
class AuthCheckResult:
    """Response of ``GET /v1/auth/check``."""

    auth_mode: str
    has_tokens: bool


@dataclass(frozen=True, slots=True)
class HealthResult:
    """Response of ``GET /health``."""

    status: str = "ok"


@dataclass(frozen=True, slots=True)
class AdminCleanupRequest:
    """Body of ``POST /v1/admin/channels/cleanup``."""

    pattern: str
    older_than: str | None = None


@dataclass(frozen=True, slots=True)
class AdminCleanupResult:
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
class AdminCompactResult:
    """Response of ``POST /v1/admin/compact``."""

    channel: str
    messages_removed: int


@dataclass(frozen=True, slots=True)
class ErrorResult:
    """Error body shared by every bus route."""

    error: str
    message: str
