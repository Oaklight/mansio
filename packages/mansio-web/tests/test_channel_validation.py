"""Tests for channel name validation in ``mansio_core.operations.validation``.

Verifies:
- User channel naming rules (length, start/end chars, specials)
- Trailing underscore rejection (consistent with other trailing specials)
- 64-char boundary (stated max is 64, not 63)
- Reserved prefix enforcement
- Unicode channel names
"""

from __future__ import annotations

import pytest
from mansio_core.operations import OperationError
from mansio_core.operations.validation import validate_channel_name


def _assert_rejected(name: str) -> None:
    with pytest.raises(OperationError) as exc:
        validate_channel_name(name)
    assert exc.value.kind == "bad_request"


class TestValidateChannelName:
    """Unit tests for validate_channel_name."""

    # --- Valid user channels ---

    @pytest.mark.parametrize(
        "name",
        [
            "abc",  # minimum length
            "general",  # typical name
            "dev-log",  # hyphens
            "test.channel",  # dots
            "test_channel",  # underscores in middle
            "a" * 64,  # maximum length
            "ab1",  # ends with digit
            "café",  # Unicode (French)
            "проект",  # Unicode (Russian)
            "中文频道",  # Unicode (Chinese)
            "テスト",  # Unicode (Japanese)
            "über-cool",  # Unicode (German)
            "my_test.chan-1",  # mixed separators
        ],
    )
    def test_valid_user_channel(self, name: str) -> None:
        validate_channel_name(name)

    # --- Invalid: length ---

    @pytest.mark.parametrize(
        "name",
        [
            "",  # empty
            "a",  # 1 char
            "ab",  # 2 chars
            "a" * 65,  # over max
            "a" * 128,  # way over max
        ],
    )
    def test_reject_bad_length(self, name: str) -> None:
        _assert_rejected(name)

    # --- Invalid: start character ---

    @pytest.mark.parametrize(
        "name",
        [
            "1starts-digit",
            "-starts-hyphen",
            ".starts-dot",
            "_starts-under",
        ],
    )
    def test_reject_bad_start(self, name: str) -> None:
        _assert_rejected(name)

    # --- Invalid: trailing special characters ---

    @pytest.mark.parametrize(
        "name",
        [
            "abc-",  # trailing hyphen
            "abc.",  # trailing dot
            "abc_",  # trailing underscore
        ],
    )
    def test_reject_trailing_special(self, name: str) -> None:
        _assert_rejected(name)

    # --- Invalid: format ---

    @pytest.mark.parametrize(
        "name",
        [
            "UPPERCASE",
            "has spaces",
            "has\ttab",
            "test::double",  # consecutive colons (won't match anyway)
            "test--double",  # consecutive hyphens
            "test..double",  # consecutive dots
            "test__double",  # consecutive underscores
            "test.-mixed",  # consecutive mixed specials
        ],
    )
    def test_reject_bad_format(self, name: str) -> None:
        _assert_rejected(name)

    # --- Reserved prefixes pass name validation (auth enforced elsewhere) ---

    def test_reserved_prefix_passes_name_validation(self) -> None:
        """broadcast: is admin-only, but validate_channel_name only checks format.

        The publish operation enforces the ACL rule.
        """
        validate_channel_name("broadcast:test")

    # --- Valid system channels ---

    @pytest.mark.parametrize(
        "name",
        [
            "_system:agents",
            "dm:alice:bob",
            "notebook:elena",
            "memory:elena",
            "broadcast:releases",
        ],
    )
    def test_valid_system_channel(self, name: str) -> None:
        validate_channel_name(name)

    # --- Invalid: trailing newline ---

    @pytest.mark.parametrize(
        "name",
        [
            "abc\n",  # plain user channel
            "general\n",  # plain user channel
            "_system:agents\n",  # system channel
            "notebook:alice\n",  # reserved prefix
            "broadcast:releases\n",  # reserved prefix
            "dm:alice:bob\n",  # newline in a DM participant slot
        ],
    )
    def test_reject_trailing_newline(self, name: str) -> None:
        """#384: a ``$`` anchor also matches before a trailing newline."""
        _assert_rejected(name)

    # --- Boundary: exactly 64 chars ---

    def test_64_char_boundary(self) -> None:
        """64 chars should be accepted (stated max), 65 rejected."""
        validate_channel_name("a" * 64)
        _assert_rejected("a" * 65)

    # --- Trailing underscore consistency ---

    def test_trailing_special_consistency(self) -> None:
        """All trailing specials (hyphen, dot, underscore) should be rejected."""
        for suffix in ["-", ".", "_"]:
            name = "test" + suffix
            with pytest.raises(OperationError):
                validate_channel_name(name)
