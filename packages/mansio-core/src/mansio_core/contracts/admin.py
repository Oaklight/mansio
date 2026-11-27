"""Typed contracts for the admin HTTP API.

Request parsers turn admin mutation bodies into dataclasses.  Response
documents are assembled by ``mansio_web.admin.view.AdminView`` and serialize
through the same ``WireResult`` base as the bus API.  Every field here is a
primitive (``str``, ``int``, ``bool``, ``dict``, ``list``) or a document made
of primitives; no store or web type crosses into core.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from mansio_core.contracts.bus import WireResult
from mansio_core.contracts.errors import ContractError
from mansio_core.types import is_valid_user_id

_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,31}$")


def _object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ContractError("Request body must be a JSON object")
    return body


def _label(data: Mapping[str, Any], default: str) -> str:
    label = data.get("label", default)
    if not isinstance(label, str):
        raise ContractError("'label' must be a string", field="label")
    return label


@dataclass(frozen=True, slots=True)
class AdminMessagesQuery:
    """Query parameters of ``GET /api/messages``."""

    channel: str
    sender: str | None = None
    msg_type: str | None = None
    limit: int = 100


@dataclass(frozen=True, slots=True)
class PrefixCreateRequest:
    """Body of ``POST /api/prefixes``."""

    prefix: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class UserCreateRequest:
    """Body of ``POST /api/users``."""

    user_id: str
    label: str = "initial token"


@dataclass(frozen=True, slots=True)
class TokenCreateRequest:
    """Body of ``POST /api/tokens``."""

    user_id: str | None = None
    label: str = ""
    supertoken: bool = False


def parse_admin_messages(params: Mapping[str, Any]) -> AdminMessagesQuery:
    """Parse ``GET /api/messages`` query parameters."""
    channel = (params.get("channel") or [None])[0]
    if not channel:
        raise ContractError("Query param 'channel' required", field="channel")
    sender = (params.get("sender") or [None])[0]
    msg_type = (params.get("msg_type") or [None])[0]
    raw_limit = (params.get("limit") or ["100"])[0]
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise ContractError("'limit' must be an integer", field="limit") from exc
    if limit < 1:
        raise ContractError("'limit' must be at least 1", field="limit")
    return AdminMessagesQuery(channel=channel, sender=sender, msg_type=msg_type, limit=limit)


def parse_prefix_create(body: Any) -> PrefixCreateRequest:
    """Parse ``POST /api/prefixes``."""
    data = _object(body)
    prefix = data.get("prefix", "")
    if not isinstance(prefix, str):
        raise ContractError(
            "Prefix must be 2-32 lowercase alphanumeric, start with letter",
            field="prefix",
        )
    prefix = prefix.strip().lower()
    if not _PREFIX_RE.match(prefix):
        raise ContractError(
            "Prefix must be 2-32 lowercase alphanumeric, start with letter",
            field="prefix",
        )
    return PrefixCreateRequest(prefix=prefix, label=_label(data, ""))


def parse_user_create(body: Any) -> UserCreateRequest:
    """Parse ``POST /api/users``."""
    data = _object(body)
    user_id = data.get("user_id", "")
    if not isinstance(user_id, str):
        raise ContractError("user_id is required", field="user_id")
    user_id = user_id.strip()
    if not user_id:
        raise ContractError("user_id is required", field="user_id")
    if not is_valid_user_id(user_id):
        raise ContractError(
            "user_id must be 3-64 chars, lowercase alphanumeric + hyphens",
            field="user_id",
        )
    return UserCreateRequest(user_id=user_id, label=_label(data, "initial token"))


def parse_token_create(body: Any) -> TokenCreateRequest:
    """Parse ``POST /api/tokens``."""
    data = _object(body)
    supertoken = data.get("supertoken", False)
    if not isinstance(supertoken, bool):
        raise ContractError("supertoken must be a boolean", field="supertoken")
    user_id = data.get("user_id")
    if supertoken:
        if user_id is not None:
            raise ContractError("user_id must be omitted when supertoken is true", field="user_id")
        return TokenCreateRequest(label=_label(data, ""), supertoken=True)
    if not isinstance(user_id, str) or not user_id.strip():
        raise ContractError("user_id must be a non-empty string", field="user_id")
    return TokenCreateRequest(user_id=user_id.strip(), label=_label(data, ""), supertoken=False)


def parse_user_token_create(body: Any) -> str:
    """Parse the label of ``POST /api/users/<user_id>/tokens``."""
    return _label(_object(body), "")


# ── Read-route response documents ────────────────────────────────


@dataclass(frozen=True, slots=True)
class AdminStatsResult(WireResult):
    """Response of ``GET /api/stats``.

    The backend defines the stat keys, so the document is the dict itself
    rather than a field of it.
    """

    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.stats)


@dataclass(frozen=True, slots=True)
class AdminThroughputResult(WireResult):
    """Response of ``GET /api/stats/throughput``."""

    window_seconds: int = 60
    buckets: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AdminChannelSummary(WireResult):
    """One entry of ``GET /api/channels``."""

    name: str
    message_count: int
    sender_count: int
    subscription_count: int
    last_message_time: str | None = None


@dataclass(frozen=True, slots=True)
class AdminChannelsResult(WireResult):
    """Response of ``GET /api/channels``."""

    channels: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AdminChannelDetail(WireResult):
    """Response of ``GET /api/channels/<name>``."""

    name: str
    message_count: int
    senders: list = field(default_factory=list)
    msg_types: list = field(default_factory=list)
    last_message_time: str | None = None


@dataclass(frozen=True, slots=True)
class AdminMessagesResult(WireResult):
    """Response of ``GET /api/messages``."""

    count: int = 0
    messages: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AdminSubscriptionsResult(WireResult):
    """Response of ``GET /api/subscriptions``."""

    total: int = 0
    channels: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AdminSystemResult(WireResult):
    """Response of ``GET /api/system``."""

    version: str = ""
    backend: dict = field(default_factory=dict)
    tokens: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdminPrefixesResult(WireResult):
    """Response of ``GET /api/prefixes``."""

    prefixes: list = field(default_factory=list)
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class AdminPrefixCreatedResult(WireResult):
    """Response of ``POST /api/prefixes``."""

    ok: bool = True
    prefix: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdminPrefixDeletedResult(WireResult):
    """Response of ``DELETE /api/prefixes/<prefix>``."""

    ok: bool = True
    deleted: str = ""


@dataclass(frozen=True, slots=True)
class AdminUsersResult(WireResult):
    """Response of ``GET /api/users``."""

    users: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AdminUserRegisteredResult(WireResult):
    """Response of ``POST /api/users``."""

    ok: bool = True
    user_id: str = ""
    token: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdminUserDetailResult(WireResult):
    """Response of ``GET /api/users/<user_id>``."""

    user_id: str
    tokens: list = field(default_factory=list)
    status: str = "unknown"
    last_seen: str | None = None


@dataclass(frozen=True, slots=True)
class AdminUserDeletedResult(WireResult):
    """Response of ``DELETE /api/users/<user_id>``."""

    ok: bool = True
    deleted_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AdminUserTokensResult(WireResult):
    """Response of ``GET /api/users/<user_id>/tokens``."""

    user_id: str
    tokens: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AdminTokenCreatedResult(WireResult):
    """Response of ``POST /api/tokens`` and ``POST /api/users/<id>/tokens``."""

    _omit_none: ClassVar[frozenset[str]] = frozenset({"supertoken"})

    ok: bool = True
    supertoken: bool | None = None
    token: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdminTokensResult(WireResult):
    """Response of ``GET /api/tokens``."""

    tokens: list = field(default_factory=list)
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class AdminTokenDeletedResult(WireResult):
    """Response of ``DELETE /api/tokens/<token_id>``."""

    ok: bool = True
    deleted: str = ""


@dataclass(frozen=True, slots=True)
class AdminTokenRotatedResult(WireResult):
    """Response of ``POST /api/tokens/<token_id>/rotate``."""

    ok: bool = True
    token: dict = field(default_factory=dict)
