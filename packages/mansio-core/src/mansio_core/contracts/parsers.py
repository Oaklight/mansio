"""Parse wire payloads into contract dataclasses.

Every parser raises :class:`ContractError` with a caller-safe message.
Route handlers catch that error and return HTTP 400, so validation lives
in one place instead of being repeated per route.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, cast

from mansio_core.contracts.bus import (
    ACLAddRequest,
    ACLEntrySpec,
    ACLSetRequest,
    AdminCleanupRequest,
    AdminCompactRequest,
    ChannelCreateRequest,
    ChannelsRequest,
    PresenceHeartbeatRequest,
    PresenceListRequest,
    PublishRequest,
    QueryRequest,
    QueueAckRequest,
    QueueClaimRequest,
    QueueStatusRequest,
    RegistryLookupRequest,
)
from mansio_core.contracts.errors import ContractError
from mansio_core.types import ALLOWED_PERMISSIONS

PUBLISH_REQUIRED = ("channel", "sender", "msg_type", "payload")
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_MSG_TYPE_LEN = 64
MAX_METADATA_BYTES = 16384
MAX_TAGS = 20
MAX_TAG_LEN = 64

QueryValue = str | Sequence[str] | None


def _fmt_bytes(n: int) -> str:
    """Format a byte count the way the API messages do."""
    if n >= 1024 * 1024 and n % (1024 * 1024) == 0:
        return f"{n // (1024 * 1024)}MB"
    if n >= 1024 and n % 1024 == 0:
        return f"{n // 1024}KB"
    return f"{n} bytes"


def _first(params: Mapping[str, QueryValue], key: str, default: str | None = None) -> str | None:
    """Return the first string value for *key* from a query-param mapping."""
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        return first if isinstance(first, str) else default
    return default


def _object(body: Any, message: str = "Request body must be a JSON object") -> dict[str, Any]:
    """Require a JSON object body."""
    if not isinstance(body, dict):
        raise ContractError(message)
    return body


def _metadata(metadata: Any) -> dict | None:
    """Validate optional metadata and normalise its tags list."""
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ContractError("metadata must be a JSON object or null", field="metadata")
    try:
        encoded = json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("Metadata must be JSON-serializable", field="metadata") from exc
    if len(encoded.encode()) > MAX_METADATA_BYTES:
        raise ContractError(
            f"Metadata exceeds maximum size of {_fmt_bytes(MAX_METADATA_BYTES)}",
            field="metadata",
        )
    if "tags" in metadata:
        tags = metadata["tags"]
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ContractError("Tags must be a list of strings", field="metadata.tags")
        tags = [tag.strip() for tag in tags]
        if not all(tags):
            raise ContractError("Tags must be non-empty strings", field="metadata.tags")
        if len(tags) > MAX_TAGS:
            raise ContractError(f"Maximum {MAX_TAGS} tags allowed", field="metadata.tags")
        if any(len(tag) > MAX_TAG_LEN for tag in tags):
            raise ContractError(
                f"Each tag must be at most {MAX_TAG_LEN} characters", field="metadata.tags"
            )
        metadata = {**metadata, "tags": tags}
    return metadata


def parse_publish(body: Any) -> PublishRequest:
    """Parse ``POST /v1/publish``."""
    data = _object(body)

    missing = [name for name in PUBLISH_REQUIRED if name not in data]
    if missing:
        raise ContractError(f"Missing: {', '.join(missing)}")

    payload = data["payload"]
    if not isinstance(payload, str) or not payload.strip():
        raise ContractError("Payload must be a non-empty string", field="payload")
    if "\x00" in payload:
        raise ContractError("Payload must not contain null bytes", field="payload")
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ContractError(
            f"Payload exceeds maximum size of {_fmt_bytes(MAX_PAYLOAD_BYTES)}", field="payload"
        )

    msg_type = data["msg_type"]
    if not isinstance(msg_type, str) or not msg_type.strip():
        raise ContractError("msg_type must be a non-empty string", field="msg_type")
    if "\x00" in msg_type:
        raise ContractError("msg_type must not contain null bytes", field="msg_type")
    msg_type = msg_type.strip()
    if len(msg_type) > MAX_MSG_TYPE_LEN:
        raise ContractError(
            f"msg_type must be at most {MAX_MSG_TYPE_LEN} characters", field="msg_type"
        )

    metadata = _metadata(data.get("metadata"))

    channel = data["channel"]
    if not isinstance(channel, str):
        raise ContractError("Channel must be a string without null bytes", field="channel")
    if "\x00" in channel:
        raise ContractError("Channel must not contain null bytes", field="channel")

    sender = data["sender"]
    if not isinstance(sender, str) or "\x00" in sender:
        raise ContractError("Sender must be a string without null bytes", field="sender")

    return PublishRequest(
        channel=channel.strip(),
        sender=sender.strip(),
        msg_type=msg_type,
        payload=payload,
        metadata=metadata,
        queue=bool(data.get("queue")),
        parent_id=data.get("parent_id"),
        intent=data.get("intent"),
    )


def _query_limit(raw: str | None, max_limit: int) -> int:
    try:
        limit = int(raw or "100")
    except ValueError as exc:
        raise ContractError("'limit' must be an integer", field="limit") from exc
    if limit < 1:
        raise ContractError("'limit' must be at least 1", field="limit")
    return min(limit, max_limit)


def _query_offset(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        offset = int(raw)
    except ValueError as exc:
        raise ContractError("'offset' must be an integer", field="offset") from exc
    if offset < 0:
        raise ContractError("'offset' must be non-negative", field="offset")
    return offset


def _query_timeout(raw: str | None, default: int = 120) -> int:
    try:
        timeout = int(raw or str(default))
    except ValueError as exc:
        raise ContractError("'timeout' must be an integer", field="timeout") from exc
    if timeout < 1:
        raise ContractError("'timeout' must be at least 1", field="timeout")
    return timeout


def parse_query(
    params: Mapping[str, QueryValue],
    *,
    max_limit: int,
    validate_channel: Callable[[str], None] | None = None,
) -> QueryRequest:
    """Parse ``GET /v1/query`` query parameters."""
    channel = _first(params, "channel")
    if not channel:
        raise ContractError("Query param 'channel' required", field="channel")
    if validate_channel is not None:
        validate_channel(channel)

    limit = _query_limit(_first(params, "limit"), max_limit)
    offset = _query_offset(_first(params, "offset"))
    raw_order = _first(params, "order", "oldest") or "oldest"
    if raw_order not in ("oldest", "newest"):
        raise ContractError("'order' must be 'oldest' or 'newest'", field="order")
    order: Literal["oldest", "newest"] = cast('Literal["oldest", "newest"]', raw_order)

    return QueryRequest(
        channel=channel,
        after=_first(params, "after"),
        limit=limit,
        order=order,
        offset=offset,
        msg_type=_first(params, "msg_type"),
        thread_id=_first(params, "thread_id"),
        intent=_first(params, "intent"),
    )


def parse_channels(params: Mapping[str, QueryValue]) -> ChannelsRequest:
    """Parse ``GET /v1/channels`` query parameters."""
    return ChannelsRequest(detail=(_first(params, "detail", "false") or "").lower() == "true")


def parse_channel_create(body: Any) -> ChannelCreateRequest:
    """Parse ``POST /v1/channels``."""
    data = _object(body)
    name = data.get("name", "")
    if not isinstance(name, str):
        raise ContractError("'name' is required", field="name")
    name = name.strip()
    if not name:
        raise ContractError("'name' is required", field="name")

    owner = data.get("owner")
    if owner is not None and not isinstance(owner, str):
        raise ContractError("'owner' must be a string", field="owner")

    visibility = data.get("visibility", "public")
    if visibility not in ("public", "private"):
        raise ContractError("visibility must be 'public' or 'private'", field="visibility")

    return ChannelCreateRequest(name=name, owner=owner, visibility=visibility)


def _permission(value: Any) -> str:
    """Validate an ACL permission string."""
    if not isinstance(value, str) or value not in ALLOWED_PERMISSIONS:
        allowed = ", ".join(sorted(ALLOWED_PERMISSIONS))
        raise ContractError(f"permission must be one of: {allowed}", field="permission")
    return value


def parse_acl_set(body: Any) -> ACLSetRequest:
    """Parse ``PUT /v1/channels/<channel>/acl``."""
    data = _object(body)
    raw_entries = data.get("acl", [])
    if not isinstance(raw_entries, list):
        raise ContractError("'acl' must be a list", field="acl")

    entries: list[ACLEntrySpec] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ContractError("Each ACL entry must be an object", field="acl")
        user_id = raw.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ContractError("Each ACL entry requires 'user_id'", field="acl.user_id")
        permission = _permission(raw.get("permission", "read"))
        entries.append(ACLEntrySpec(user_id=user_id, permission=permission))

    return ACLSetRequest(entries=entries)


def parse_acl_add(body: Any) -> ACLAddRequest:
    """Parse ``POST /v1/channels/<channel>/acl``."""
    data = _object(body)
    user_id = data.get("user_id", "")
    if not isinstance(user_id, str):
        raise ContractError("'user_id' is required", field="user_id")
    user_id = user_id.strip()
    if not user_id:
        raise ContractError("'user_id' is required", field="user_id")
    permission = _permission(data.get("permission", "read"))
    return ACLAddRequest(user_id=user_id, permission=permission)


def _queue_body(body: Any, field: str) -> tuple[dict[str, Any], str]:
    data = _object(body, "Invalid JSON")
    value = data.get(field, "")
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"Missing '{field}'", field=field)
    return data, value.strip()


def parse_queue_claim(body: Any) -> QueueClaimRequest:
    """Parse ``POST /v1/queue/claim``."""
    data, channel = _queue_body(body, "channel")

    claimed_by = data.get("claimed_by")
    if claimed_by is not None:
        if not isinstance(claimed_by, str):
            raise ContractError("'claimed_by' must be a string", field="claimed_by")
        claimed_by = claimed_by.strip() or None

    raw_lease = data.get("lease_seconds", 300)
    try:
        lease = int(raw_lease)
    except (TypeError, ValueError) as exc:
        raise ContractError("'lease_seconds' must be an integer", field="lease_seconds") from exc

    return QueueClaimRequest(channel=channel, claimed_by=claimed_by, lease_seconds=lease)


def parse_queue_ack(body: Any) -> QueueAckRequest:
    """Parse ``POST /v1/queue/ack``."""
    data, message_id = _queue_body(body, "message_id")

    claimed_by = data.get("claimed_by")
    if claimed_by is not None:
        if not isinstance(claimed_by, str):
            raise ContractError("'claimed_by' must be a string", field="claimed_by")
        claimed_by = claimed_by.strip() or None

    return QueueAckRequest(message_id=message_id, claimed_by=claimed_by)


def parse_queue_status(params: Mapping[str, QueryValue]) -> QueueStatusRequest:
    """Parse ``GET /v1/queue/status``."""
    message_id = _first(params, "message_id")
    if not message_id:
        raise ContractError("Query param 'message_id' required", field="message_id")
    return QueueStatusRequest(message_id=message_id)


def parse_heartbeat(body: Any) -> PresenceHeartbeatRequest:
    """Parse ``POST /v1/presence/heartbeat``."""
    data = _object(body, "JSON body required")
    user_id = data.get("user_id", "")
    if not isinstance(user_id, str):
        raise ContractError("user_id required", field="user_id")
    user_id = user_id.strip()
    if not user_id:
        raise ContractError("user_id required", field="user_id")
    return PresenceHeartbeatRequest(user_id=user_id, metadata=_metadata(data.get("metadata")))


def parse_presence_list(params: Mapping[str, QueryValue]) -> PresenceListRequest:
    """Parse ``GET /v1/presence`` and ``/v1/presence/<user_id>``."""
    return PresenceListRequest(timeout=_query_timeout(_first(params, "timeout")))


def parse_registry_lookup(params: Mapping[str, QueryValue]) -> RegistryLookupRequest:
    """Parse ``GET /v1/registry/lookup``."""
    user_id = (_first(params, "user_id", "") or "").strip()
    if not user_id:
        raise ContractError("Query param 'user_id' required", field="user_id")
    return RegistryLookupRequest(user_id=user_id)


def parse_admin_cleanup(body: Any) -> AdminCleanupRequest:
    """Parse ``POST /v1/admin/channels/cleanup``."""
    data = _object(body)
    pattern = data.get("pattern", "")
    if not isinstance(pattern, str):
        raise ContractError("'pattern' field is required (e.g. 'test:*')", field="pattern")
    pattern = pattern.strip()
    if not pattern:
        raise ContractError("'pattern' field is required (e.g. 'test:*')", field="pattern")

    older_than = data.get("older_than")
    if older_than is not None and not isinstance(older_than, str):
        raise ContractError("'older_than' must be a string", field="older_than")

    return AdminCleanupRequest(pattern=pattern, older_than=older_than)


def parse_admin_compact(body: Any) -> AdminCompactRequest:
    """Parse ``POST /v1/admin/compact``."""
    data = _object(body)
    channel = data.get("channel", "")
    if not isinstance(channel, str):
        raise ContractError("'channel' field is required", field="channel")
    channel = channel.strip()
    if not channel:
        raise ContractError("'channel' field is required", field="channel")

    max_messages = data.get("max_messages")
    if max_messages is not None and (not isinstance(max_messages, int) or max_messages < 1):
        raise ContractError("max_messages must be a positive integer", field="max_messages")

    return AdminCompactRequest(
        channel=channel,
        max_messages=max_messages,
        keep_latest_per_sender=bool(data.get("keep_latest_per_sender", False)),
    )
