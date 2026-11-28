"""The stable agent-entry tool subset (docs/AGENT_ENTRY.md, A2)."""

from __future__ import annotations

import pytest
from mansio_client.mcp import _TOOLS

# Tool name -> required argument names. Renaming a tool or dropping an
# argument breaks the agent entry contract and this test.
STABLE_REQUIRED: dict[str, tuple[str, ...]] = {
    "mansio_send": ("channel", "content"),
    "mansio_read": ("channel",),
    "mansio_poll": ("channel",),
    "mansio_wait": ("channel",),
    "mansio_check_unread": (),
    "mansio_heartbeat": (),
    "mansio_agents": (),
    "mansio_channels": (),
    "mansio_dm_send": ("to_user", "content"),
    "mansio_dm_read": ("with_user",),
    "mansio_queue_publish": ("channel", "content"),
    "mansio_queue_claim": ("channel",),
    "mansio_queue_ack": ("message_id",),
    "mansio_queue_status": ("message_id",),
}


def _tools_by_name() -> dict[str, dict]:
    names = [tool["name"] for tool in _TOOLS]
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"
    return {tool["name"]: tool for tool in _TOOLS}


@pytest.mark.parametrize("name", sorted(STABLE_REQUIRED))
def test_stable_tool_is_exposed_once(name: str) -> None:
    assert [tool["name"] for tool in _TOOLS].count(name) == 1


@pytest.mark.parametrize(("name", "required"), sorted(STABLE_REQUIRED.items()))
def test_required_arguments_match_the_contract(name: str, required: tuple[str, ...]) -> None:
    schema = _tools_by_name()[name]["inputSchema"]
    declared_required = set(schema.get("required", []))
    assert declared_required == set(required), (
        f"{name}: required arguments are {sorted(declared_required)}, "
        f"the contract says {sorted(required)}"
    )
    assert set(required) <= set(schema.get("properties", {})), (
        f"{name}: a required argument has no schema property"
    )
