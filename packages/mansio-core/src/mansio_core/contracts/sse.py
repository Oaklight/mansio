"""Typed SSE stream envelope for bus subscriptions.

Each ``data:`` line of a subscribe stream carries one JSON object::

    {"channel": "general", "message": {...}}

:class:`StreamEvent` is that document.  ``message_to_dict`` is also the
REST message serializer (the web frontend delegates to it), and the parse
functions are what both HTTP transports use, so the wire definitions
cannot drift.  No backend-internal type appears here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mansio_core.contracts.errors import ContractError
from mansio_core.types import Message

_REQUIRED_MESSAGE_FIELDS = ("id", "channel", "sender", "msg_type", "payload", "timestamp")
_OPTIONAL_MESSAGE_FIELDS = ("parent_id", "thread_id", "intent")


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One subscription event: the channel and the delivered message."""

    channel: str
    message: Message


def message_to_dict(message: Message) -> dict[str, Any]:
    """Serialize a message to its wire object.

    ``metadata`` is always present (``null`` when unset); optional string
    fields are omitted when unset, matching the pre-existing wire bodies.
    """
    data: dict[str, Any] = {
        "id": message.id,
        "channel": message.channel,
        "sender": message.sender,
        "msg_type": message.msg_type,
        "payload": message.payload,
        "timestamp": message.timestamp,
        "metadata": message.metadata,
    }
    for field in _OPTIONAL_MESSAGE_FIELDS:
        value = getattr(message, field)
        if value is not None:
            data[field] = value
    return data


def message_from_dict(data: Any) -> Message:
    """Build a :class:`Message` from its wire object.

    Every required field must be a non-empty string.  The publish contract
    and ``Bus.publish`` enforce the same field guarantees: required fields
    as non-empty strings, ``parent_id`` and ``intent`` as strings or
    ``None``, and ``metadata`` as a dict or ``None``, with ``Bus.publish``
    raising ``ValueError``.  Any message either path produces therefore
    parses here.

    Raises :class:`ContractError` when a required field is missing or the
    wrong type.
    """
    if not isinstance(data, Mapping):
        raise ContractError("Stream event 'message' must be a JSON object")
    for field in _REQUIRED_MESSAGE_FIELDS:
        if not isinstance(data.get(field), str) or not data[field]:
            raise ContractError(f"Stream event message requires '{field}'", field=field)
    for field in _OPTIONAL_MESSAGE_FIELDS:
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            raise ContractError(f"Stream event message '{field}' must be a string", field=field)
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ContractError(
            "Stream event message 'metadata' must be a JSON object", field="metadata"
        )
    return Message(
        id=data["id"],
        channel=data["channel"],
        sender=data["sender"],
        msg_type=data["msg_type"],
        payload=data["payload"],
        timestamp=data["timestamp"],
        metadata=metadata,
        parent_id=data.get("parent_id"),
        thread_id=data.get("thread_id"),
        intent=data.get("intent"),
    )


def parse_stream_event(data: str) -> StreamEvent:
    """Parse one SSE ``data:`` payload into a :class:`StreamEvent`.

    Raises :class:`ContractError` for malformed JSON or a document that
    does not match the envelope.
    """
    try:
        document = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ContractError("Stream event must be valid JSON") from exc
    if not isinstance(document, Mapping):
        raise ContractError("Stream event must be a JSON object")
    channel = document.get("channel")
    if not isinstance(channel, str) or not channel:
        raise ContractError("Stream event requires 'channel'", field="channel")
    if "message" not in document:
        raise ContractError("Stream event requires 'message'", field="message")
    return StreamEvent(channel=channel, message=message_from_dict(document["message"]))


def stream_event_to_json(event: StreamEvent) -> str:
    """Serialize a :class:`StreamEvent` to its SSE ``data:`` payload."""
    return json.dumps(
        {"channel": event.channel, "message": message_to_dict(event.message)},
        ensure_ascii=False,
    )
