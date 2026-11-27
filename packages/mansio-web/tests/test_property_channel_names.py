"""Property-based tests for plain channel-name validation.

The enumerated cases in ``test_channel_validation.py`` stay in place as named
examples and regressions.  These tests state the same rule as invariants over
generated names, so a new character class or boundary is checked without
editing a case list.

The strategy generates names from the documented grammar.  The oracle is an
independent statement of that rule, not a copy of the validator.
"""

from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st
from mansio_web.frontends.http import _validate_channel_name

# Plain channel names: 3-64 chars, lowercase, first char a letter, last char
# alphanumeric, no colon, no consecutive separators.
_PLAIN_FIRST = "abcdefghijklmnopqrstuvwxyz"
_PLAIN_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789.-"
_PLAIN_ENDS = "abcdefghijklmnopqrstuvwxyz0123456789"
_CONSECUTIVE_SPECIALS = re.compile(r"[._-]{2}")

valid_plain_channels = st.builds(
    lambda first, middle, last: first + middle + last,
    st.sampled_from(_PLAIN_FIRST),
    st.text(alphabet=_PLAIN_CHARS, min_size=1, max_size=62),
    st.sampled_from(_PLAIN_ENDS),
).filter(lambda name: _CONSECUTIVE_SPECIALS.search(name) is None)


def _spec_plain_channel(name: str) -> bool:
    """Independent statement of the plain-channel naming rule."""
    return (
        3 <= len(name) <= 64
        and name[0] in _PLAIN_FIRST
        and all(char in _PLAIN_CHARS for char in name)
        and name[-1] in _PLAIN_ENDS
        and _CONSECUTIVE_SPECIALS.search(name) is None
    )


@given(valid_plain_channels)
def test_generated_plain_channel_names_are_accepted(name: str) -> None:
    assert _spec_plain_channel(name)
    assert _validate_channel_name(name) is None


@given(valid_plain_channels)
def test_plain_channel_name_mutations_are_rejected(name: str) -> None:
    mutations = [
        name[:2],
        name + "a" * (65 - len(name)),
        "9" + name,
        "_" + name,
        name.upper() if name.upper() != name else name + "A",
        name + "-",
        name + ".",
        name + "_",
        name[:1] + ".." + name[1:],
        "x:" + name,
    ]
    for mutated in mutations:
        assert _validate_channel_name(mutated) is not None, mutated


def test_plain_channel_name_with_trailing_newline_is_rejected() -> None:
    assert _validate_channel_name("abc\n") is not None
