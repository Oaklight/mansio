"""Property-based tests for the permission algebra and the ``dm:`` grammar.

The enumerated tests in ``test_permission_verbs.py``,
``test_dm_channel_validation.py``, and ``test_channel_access.py`` stay in
place as named examples and regressions.  These tests state the same rules as
invariants over generated inputs, so a new alias, preset, or id shape is
checked without editing a hand-written case list.

The strategies generate strings from the real grammar (the agent-id character
rules), not arbitrary text.  Oracles are independent statements of the
documented rule, not copies of the implementation.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from mansio_core.authz import (
    dm_channel_error,
    dm_participants,
    is_channel_member,
    is_private_channel,
)
from mansio_core.types import (
    ALLOWED_PERMISSIONS,
    PERMISSION_ALIASES,
    REQUIRED_ALIASES,
    ROLE_PRESETS,
    VERBS,
    expand_permission,
    is_valid_user_id,
    normalize_required,
    permission_allows,
)

# ── strategies over the documented grammar ───────────────────────────────

# Agent ids: 3-64 chars, lowercase alphanumerics and hyphens, first and last
# alphanumeric.  A middle of 1-62 chars plus the two ends covers 3-64.
_ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"
_ID_ENDS = "abcdefghijklmnopqrstuvwxyz0123456789"

valid_ids = st.builds(
    lambda first, middle, last: first + middle + last,
    st.sampled_from(_ID_ENDS),
    st.text(alphabet=_ID_CHARS, min_size=1, max_size=62),
    st.sampled_from(_ID_ENDS),
)

# Mutations of the id grammar.  Each one is invalid under the documented rule,
# so the oracle never has to guess why it was rejected.
invalid_ids = st.one_of(
    st.text(alphabet=_ID_CHARS, max_size=2),
    st.text(alphabet=_ID_CHARS, min_size=65, max_size=70),
    st.builds(lambda value: "-" + value, valid_ids),
    st.builds(lambda value: value + "-", valid_ids),
    st.builds(lambda value: "A" + value, valid_ids),
    st.builds(lambda value: value + ".", valid_ids),
    st.builds(lambda value: value + "_", valid_ids),
    st.builds(lambda value: value + " ", valid_ids),
    st.builds(lambda value: value + "\t", valid_ids),
    st.builds(lambda value: value + "\x00", valid_ids),
)

segments = st.one_of(valid_ids, invalid_ids)


def _spec_valid_user_id(value: str) -> bool:
    """Independent statement of the user-id rule."""
    return (
        3 <= len(value) <= 64
        and all(char in _ID_CHARS for char in value)
        and value[0] in _ID_ENDS
        and value[-1] in _ID_ENDS
    )


def _dm(parts: list[str]) -> str:
    return "dm:" + ":".join(parts)


# ── user ids ─────────────────────────────────────────────────────────────


@given(valid_ids)
def test_generated_ids_satisfy_the_user_id_rule(user_id: str) -> None:
    assert _spec_valid_user_id(user_id)
    assert is_valid_user_id(user_id)


@given(invalid_ids)
def test_mutated_ids_are_rejected(user_id: str) -> None:
    assert not _spec_valid_user_id(user_id)
    assert not is_valid_user_id(user_id)


@given(valid_ids)
def test_id_with_trailing_newline_is_rejected(user_id: str) -> None:
    assert not is_valid_user_id(user_id + "\n")
    assert dm_participants(f"dm:{user_id}\n:bob") is None


# ── dm parsing and membership ────────────────────────────────────────────


@given(valid_ids, valid_ids)
def test_well_formed_dm_names_parse_exactly(first: str, second: str) -> None:
    channel = f"dm:{first}:{second}"
    assert dm_participants(channel) == (first, second)
    assert dm_channel_error(channel) is None
    assert is_private_channel(channel)


@given(valid_ids, valid_ids, valid_ids)
def test_membership_agrees_with_dm_participants(user_id: str, first: str, second: str) -> None:
    participants = dm_participants(f"dm:{first}:{second}")
    assert participants == (first, second)
    assert is_channel_member(user_id, f"dm:{first}:{second}") is (user_id in participants)


@given(
    st.one_of(
        st.lists(segments, max_size=1).map(_dm),
        st.lists(segments, min_size=3, max_size=5).map(_dm),
        st.tuples(segments, segments)
        .filter(lambda pair: not (_spec_valid_user_id(pair[0]) and _spec_valid_user_id(pair[1])))
        .map(lambda pair: f"dm:{pair[0]}:{pair[1]}"),
    )
)
def test_malformed_dm_names_are_rejected(channel: str) -> None:
    assert dm_participants(channel) is None
    assert dm_channel_error(channel) is not None
    assert is_private_channel(channel)
    assert not is_channel_member("alice", channel)


@given(st.text().filter(lambda value: not value.startswith("dm:")))
def test_non_dm_names_are_left_alone(channel: str) -> None:
    assert dm_participants(channel) is None
    assert dm_channel_error(channel) is None


# ── permission algebra ───────────────────────────────────────────────────

_REQUIRED_NAMES = frozenset(VERBS) | frozenset(REQUIRED_ALIASES)


@given(st.sampled_from(sorted(ALLOWED_PERMISSIONS)))
def test_every_allowed_permission_expands_to_verbs(permission: str) -> None:
    verbs = expand_permission(permission)
    assert isinstance(verbs, frozenset)
    assert verbs
    assert verbs <= VERBS


@given(st.sampled_from(sorted(ALLOWED_PERMISSIONS)))
def test_owner_preset_is_the_most_permissive(permission: str) -> None:
    assert expand_permission(permission) <= expand_permission("owner")


@given(st.sampled_from(sorted(PERMISSION_ALIASES)))
def test_aliases_grant_the_same_verbs_as_their_preset(legacy: str) -> None:
    assert expand_permission(legacy) == expand_permission(PERMISSION_ALIASES[legacy])


def test_presets_form_a_monotone_chain() -> None:
    assert expand_permission("viewer") < expand_permission("member")
    assert expand_permission("member") < expand_permission("owner")


@given(st.text().filter(lambda value: value not in ALLOWED_PERMISSIONS))
def test_unknown_permissions_grant_nothing(permission: str) -> None:
    assert expand_permission(permission) == frozenset()


@given(st.sampled_from(sorted(ALLOWED_PERMISSIONS)), st.sampled_from(sorted(_REQUIRED_NAMES)))
def test_permission_allows_agrees_with_verb_membership(permission: str, required: str) -> None:
    assert permission_allows(permission, required) is (
        normalize_required(required) in expand_permission(permission)
    )


@given(st.text())
def test_normalize_required_is_idempotent(required: str) -> None:
    once = normalize_required(required)
    assert normalize_required(once) == once


@given(st.sampled_from(sorted(_REQUIRED_NAMES)))
def test_normalize_required_returns_a_verb(required: str) -> None:
    assert normalize_required(required) in VERBS


@given(
    st.sampled_from(sorted(ALLOWED_PERMISSIONS)),
    st.text().filter(lambda value: value not in _REQUIRED_NAMES),
)
def test_unknown_required_names_are_never_granted(permission: str, required: str) -> None:
    assert permission_allows(permission, required) is False


def test_role_presets_are_defined_over_known_verbs() -> None:
    assert set(ROLE_PRESETS) == {"viewer", "member", "owner"}
    for verbs in ROLE_PRESETS.values():
        assert verbs <= VERBS
