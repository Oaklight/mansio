"""Tests for the SSE stream-event contract."""

from __future__ import annotations

import json

import pytest
from mansio_core.contracts import (
    ContractError,
    StreamEvent,
    WarningEvent,
    message_from_dict,
    message_to_dict,
    parse_publish,
    parse_stream_event,
    parse_warning_event,
    stream_event_to_json,
    warning_event_to_json,
)
from mansio_core.types import Message


def _message(**overrides) -> Message:
    fields = {
        "id": "msg-1",
        "channel": "general",
        "sender": "alice",
        "msg_type": "text",
        "payload": "hello",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    fields.update(overrides)
    return Message(**fields)


class TestMessageWireObject:
    def test_optional_fields_are_omitted_when_unset(self) -> None:
        assert message_to_dict(_message()) == {
            "id": "msg-1",
            "channel": "general",
            "sender": "alice",
            "msg_type": "text",
            "payload": "hello",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "metadata": None,
        }

    def test_optional_fields_are_included_when_set(self) -> None:
        wire = message_to_dict(
            _message(
                metadata={"tags": ["a"]},
                parent_id="parent",
                thread_id="root",
                intent="FYI_ONLY",
            )
        )
        assert wire["metadata"] == {"tags": ["a"]}
        assert wire["parent_id"] == "parent"
        assert wire["thread_id"] == "root"
        assert wire["intent"] == "FYI_ONLY"

    def test_round_trip(self) -> None:
        original = _message(metadata={"k": "v"}, parent_id="p", intent="REQUIRES_RESPONSE")
        assert message_from_dict(message_to_dict(original)) == original

    def test_parse_accepts_absent_optional_fields(self) -> None:
        message = message_from_dict(
            {
                "id": "m",
                "channel": "c",
                "sender": "s",
                "msg_type": "t",
                "payload": "p",
                "timestamp": "ts",
            }
        )
        assert message.metadata is None
        assert message.parent_id is None

    def test_empty_producer_controlled_fields_are_rejected(self) -> None:
        """The publish contract rejects these, so the parser may reject them too."""
        for field in ("channel", "sender", "msg_type", "payload"):
            with pytest.raises(ContractError, match=f"requires '{field}'"):
                message_from_dict(
                    {
                        "id": "m",
                        "channel": "c",
                        "sender": "s",
                        "msg_type": "t",
                        "payload": "p",
                        "timestamp": "ts",
                        field: "",
                    }
                )

    def test_extra_fields_are_ignored(self) -> None:
        message = message_from_dict(
            {
                "id": "m",
                "channel": "c",
                "sender": "s",
                "msg_type": "t",
                "payload": "p",
                "timestamp": "ts",
                "future_field": 1,
            }
        )
        assert message.id == "m"

    @pytest.mark.parametrize(
        ("wire", "match"),
        [
            ([], "'message' must be a JSON object"),
            ({"channel": "c"}, "requires 'id'"),
            ({"id": "", "channel": "c"}, "requires 'id'"),
            ({"id": 1}, "requires 'id'"),
            (
                {"id": "m", "channel": "c", "sender": "s", "msg_type": "t", "payload": "p"},
                "requires 'timestamp'",
            ),
            (
                {
                    "id": "m",
                    "channel": "c",
                    "sender": "s",
                    "msg_type": "t",
                    "payload": "p",
                    "timestamp": "",
                },
                "requires 'timestamp'",
            ),
            (
                {
                    "id": "m",
                    "channel": "c",
                    "sender": "s",
                    "msg_type": "t",
                    "payload": "p",
                    "timestamp": "ts",
                    "parent_id": 1,
                },
                "'parent_id' must be a string",
            ),
            (
                {
                    "id": "m",
                    "channel": "c",
                    "sender": "s",
                    "msg_type": "t",
                    "payload": "p",
                    "timestamp": "ts",
                    "metadata": [],
                },
                "'metadata' must be a JSON object",
            ),
        ],
    )
    def test_invalid_message_objects(self, wire, match: str) -> None:
        with pytest.raises(ContractError, match=match):
            message_from_dict(wire)


class TestStreamEventDocument:
    def test_serialize_shape(self) -> None:
        event = StreamEvent(channel="general", message=_message())
        document = json.loads(stream_event_to_json(event))
        assert document == {"channel": "general", "message": message_to_dict(_message())}

    def test_unicode_is_not_escaped(self) -> None:
        event = StreamEvent(channel="general", message=_message(payload="你好"))
        assert "你好" in stream_event_to_json(event)

    def test_round_trip(self) -> None:
        event = StreamEvent(channel="general", message=_message(intent="FYI_ONLY"))
        assert parse_stream_event(stream_event_to_json(event)) == event

    @pytest.mark.parametrize(
        ("data", "match"),
        [
            ("{", "must be valid JSON"),
            ("[]", "must be a JSON object"),
            ('{"message": {}}', "requires 'channel'"),
            ('{"channel": "", "message": {}}', "requires 'channel'"),
            ('{"channel": "c"}', "requires 'message'"),
        ],
    )
    def test_invalid_envelopes(self, data: str, match: str) -> None:
        with pytest.raises(ContractError, match=match):
            parse_stream_event(data)


class TestPublishToStreamRoundTrip:
    """Field values the publish contract accepts also parse as a stream event."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"parent_id": None, "intent": None},
            {"parent_id": "parent-1", "intent": "REQUIRES_RESPONSE"},
            {"metadata": {"tags": [" a ", "b"]}},
        ],
    )
    def test_accepted_publish_round_trips(self, overrides: dict) -> None:
        body = {
            "channel": "general",
            "sender": " alice ",
            "msg_type": "text",
            "payload": "hello",
        }
        body.update(overrides)
        request = parse_publish(body)
        message = Message(
            id="msg-1",
            channel=request.channel,
            sender=request.sender,
            msg_type=request.msg_type,
            payload=request.payload,
            timestamp="2026-01-01T00:00:00+00:00",
            metadata=request.metadata,
            parent_id=request.parent_id,
            thread_id=request.parent_id,
            intent=request.intent,
        )
        event = StreamEvent(channel=request.channel, message=message)
        assert parse_stream_event(stream_event_to_json(event)) == event


class TestWarningEvent:
    def test_dropped_round_trip(self) -> None:
        event = WarningEvent(kind="dropped", count=3)
        payload = warning_event_to_json(event)
        assert json.loads(payload) == {"kind": "dropped", "count": 3, "channel": None}
        assert parse_warning_event(payload) == event

    def test_truncated_round_trip_with_channel(self) -> None:
        event = WarningEvent(kind="truncated", count=500, channel="general")
        payload = warning_event_to_json(event)
        assert json.loads(payload) == {
            "kind": "truncated",
            "count": 500,
            "channel": "general",
        }
        assert parse_warning_event(payload) == event

    @pytest.mark.parametrize("kind", ["warning", "", "DROPPED"])
    def test_unknown_kind_is_rejected(self, kind: str) -> None:
        with pytest.raises(ContractError, match="'kind'"):
            parse_warning_event(json.dumps({"kind": kind, "count": 1, "channel": None}))

    @pytest.mark.parametrize("count", [0, -1, True, "2", 1.5, None])
    def test_non_positive_or_non_integer_count_is_rejected(self, count: object) -> None:
        with pytest.raises(ContractError, match="'count'"):
            parse_warning_event(json.dumps({"kind": "dropped", "count": count, "channel": None}))

    def test_truncated_requires_channel(self) -> None:
        with pytest.raises(ContractError, match="requires 'channel'"):
            parse_warning_event(json.dumps({"kind": "truncated", "count": 1, "channel": None}))

    def test_dropped_rejects_channel(self) -> None:
        with pytest.raises(ContractError, match="must not carry 'channel'"):
            parse_warning_event(json.dumps({"kind": "dropped", "count": 1, "channel": "general"}))

    @pytest.mark.parametrize("channel", ["", 3, []])
    def test_bad_channel_is_rejected(self, channel: object) -> None:
        with pytest.raises(ContractError, match="'channel'"):
            parse_warning_event(json.dumps({"kind": "truncated", "count": 1, "channel": channel}))

    def test_malformed_json_is_rejected(self) -> None:
        with pytest.raises(ContractError, match="valid JSON"):
            parse_warning_event("{not json")

    def test_serializer_rejects_invalid_event(self) -> None:
        with pytest.raises(ContractError, match="'count'"):
            warning_event_to_json(WarningEvent(kind="dropped", count=0))
